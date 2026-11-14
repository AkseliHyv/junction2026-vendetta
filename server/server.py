#!/usr/bin/env python3
"""Map server. Stores what the boxes upload and serves the map in static/.

    python3 server/server.py              # http://localhost:8080
    python3 server/server.py --no-seed    # don't add simulated events to an empty database

    POST /api/readings   analysis payload from a box (analysis.py), stored as-is
    GET  /api/readings   ?since=<ISO time>  ->  {"payloads": [...]}   (read by the map)
                         Readings come back summarised per station: 5-minute windows for
                         the last hour, hourly before that. &raw=1 returns the payloads
                         as the boxes sent them (newest 5000).
    POST /api/events     {"device_id", "lat", "lon", "events": [...]} from events mode
    GET  /api/events     ?hours=N  ->  {"events": [...], "devices": [...]}

Events are keyed on (device_id, event id), so re-sending an event after a
"woke me" press updates it. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
LABELS = ("traffic", "construction", "music", "voices", "background", "unknown")
MAX_BODY = 1_000_000
AGG_RES = (300, 3600)  # summary windows kept per station: 5 minutes and 1 hour
AGG_KEEP_DAYS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    device_id   TEXT    NOT NULL,
    event_id    INTEGER NOT NULL,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    start_time  TEXT    NOT NULL,
    duration_ms INTEGER NOT NULL,
    label       TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    avg_level   REAL    NOT NULL,
    peak_level  REAL    NOT NULL,
    woke_me     INTEGER NOT NULL,
    tamper      INTEGER NOT NULL,
    simulated   INTEGER NOT NULL DEFAULT 0,
    received_at REAL    NOT NULL,
    PRIMARY KEY (device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
CREATE TABLE IF NOT EXISTS readings (
    station_id  TEXT NOT NULL,
    first_ts    TEXT NOT NULL,          -- earliest reading "minute" in the payload (UTC ISO)
    last_ts     TEXT NOT NULL,          -- latest reading "minute" in the payload (UTC ISO)
    payload     TEXT NOT NULL,          -- device payload exactly as received
    received_at REAL NOT NULL,
    PRIMARY KEY (station_id, first_ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_last ON readings(last_ts);
-- Per-station summaries for the map. Sums are weighted by valid_s, so readings
-- of any length combine like the map does it (energy average for LAeq).
CREATE TABLE IF NOT EXISTS agg (
    res         INTEGER NOT NULL,       -- window length, s
    station_id  TEXT    NOT NULL,
    start_ts    TEXT    NOT NULL,       -- window start (UTC ISO)
    lat         REAL,
    lon         REAL,
    duration_s  REAL    NOT NULL,
    valid_s     REAL    NOT NULL,
    e_sum       REAL    NOT NULL,       -- sum of valid_s * 10^(laeq/10)
    l90_sum     REAL    NOT NULL,       -- sum of valid_s * l90
    lmax        REAL,
    src         TEXT    NOT NULL,       -- {label: sum of valid_s * score}
    flags       TEXT    NOT NULL,       -- {flag: count}
    n           INTEGER NOT NULL,
    PRIMARY KEY (res, station_id, start_ts)
);
CREATE INDEX IF NOT EXISTS idx_agg_start ON agg(res, start_ts);
"""


def _window_start(ts: str, res: int) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    t = int(dt.timestamp()) // res * res
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _contributions(payload: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """What a payload adds to each summary window."""
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for r in payload.get("readings") or []:
        try:
            laeq = float(r["laeq_db"])
            ts = iso_utc(str(r["minute"]))
        except (KeyError, TypeError, ValueError):
            continue
        dur = float(r.get("duration_s") or 60.0)
        w = float(r["valid_s"]) if isinstance(r.get("valid_s"), (int, float)) else dur
        for res in AGG_RES:
            c = out.setdefault((res, _window_start(ts, res)), {
                "duration_s": 0.0, "valid_s": 0.0, "e_sum": 0.0, "l90_sum": 0.0, "lmax": None,
                "src": {}, "flags": {}, "n": 0})
            c["duration_s"] += dur
            c["valid_s"] += w
            c["e_sum"] += w * 10 ** (laeq / 10)
            if isinstance(r.get("l90_db"), (int, float)):
                c["l90_sum"] += w * float(r["l90_db"])
            if isinstance(r.get("lmax_db"), (int, float)):
                c["lmax"] = max(c["lmax"] or -math.inf, float(r["lmax_db"]))
            for k, v in (r.get("sources") or {}).items():
                if isinstance(v, (int, float)):
                    c["src"][k] = c["src"].get(k, 0.0) + w * v
            for f in r.get("flags") or []:
                c["flags"][str(f)] = c["flags"].get(str(f), 0) + 1
            c["n"] += 1
    return out


def iso_utc(ts: str) -> str:
    """Any ISO timestamp as UTC "YYYY-MM-DDTHH:MM:SS.mmmZ", so strings sort by time."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MapStore:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._inserts = 0
        self.raw_hours: Optional[float] = None
        if self._conn.execute("SELECT 1 FROM agg LIMIT 1").fetchone() is None:
            self._rebuild_agg()

    def _rebuild_agg(self) -> None:
        """Summaries for readings stored before the agg table existed."""
        n = 0
        with self._lock:
            for row in self._conn.execute("SELECT payload FROM readings ORDER BY first_ts").fetchall():
                p = json.loads(row["payload"])
                self._apply(str(p.get("station_id"))[:64], p.get("lat"), p.get("lon"), _contributions(p), 1)
                n += 1
            self._conn.commit()
        if n:
            print(f"[Noise Map] summarised {n} stored payloads")

    def _apply(self, station: str, lat: Any, lon: Any, contrib: Dict[Tuple[int, str], Dict[str, Any]], sign: int) -> None:
        """Add (sign=1) or take back (sign=-1) a payload's contribution. Caller holds the lock."""
        lat = float(lat) if isinstance(lat, (int, float)) else None
        lon = float(lon) if isinstance(lon, (int, float)) else None
        for (res, start), c in contrib.items():
            old = self._conn.execute(
                "SELECT * FROM agg WHERE res=? AND station_id=? AND start_ts=?", (res, station, start)).fetchone()
            if old is None:
                if sign < 0:
                    continue
                src, flags, vals = c["src"], c["flags"], c
                lmax = c["lmax"]
            else:
                src, flags = json.loads(old["src"]), json.loads(old["flags"])
                for k, v in c["src"].items():
                    src[k] = max(0.0, src.get(k, 0.0) + sign * v)
                for k, v in c["flags"].items():
                    flags[k] = flags.get(k, 0) + sign * v
                    if flags[k] <= 0:
                        del flags[k]
                vals = {k: max(0.0, old[k] + sign * c[k]) for k in ("duration_s", "valid_s", "e_sum", "l90_sum", "n")}
                lmax = old["lmax"] if sign < 0 or c["lmax"] is None else max(old["lmax"] or -math.inf, c["lmax"])
            self._conn.execute(
                """INSERT OR REPLACE INTO agg (res, station_id, start_ts, lat, lon, duration_s, valid_s, e_sum,
                                               l90_sum, lmax, src, flags, n)
                   VALUES (?, ?, ?, COALESCE(?, (SELECT lat FROM agg WHERE res=? AND station_id=? AND start_ts=?)),
                           COALESCE(?, (SELECT lon FROM agg WHERE res=? AND station_id=? AND start_ts=?)),
                           ?, ?, ?, ?, ?, ?, ?, ?)""",
                (res, station, start, lat, res, station, start, lon, res, station, start,
                 vals["duration_s"], vals["valid_s"], vals["e_sum"], vals["l90_sum"], lmax,
                 json.dumps(src), json.dumps(flags), int(vals["n"])),
            )

    def _prune(self) -> None:
        """Caller holds the lock."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=AGG_KEEP_DAYS)
        self._conn.execute("DELETE FROM agg WHERE start_ts < ?", (cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"),))
        if self.raw_hours:
            raw_cut = datetime.now(timezone.utc) - timedelta(hours=self.raw_hours)
            self._conn.execute("DELETE FROM readings WHERE last_ts < ?", (raw_cut.strftime("%Y-%m-%dT%H:%M:%S.000Z"),))

    def upsert(self, device_id: str, lat: float, lon: float, events: List[Dict[str, Any]], simulated: bool = False) -> int:
        rows = []
        now = time.time()
        for ev in events:
            label = str(ev.get("label", "unknown"))
            rows.append((
                device_id,
                int(ev["id"]),
                float(ev.get("lat", lat)),
                float(ev.get("lon", lon)),
                str(ev["start_time"]),
                max(0, int(ev.get("duration_ms", 0))),
                label if label in LABELS else "unknown",
                float(ev.get("confidence", 0.0)),
                float(ev.get("avg_level", 0.0)),
                float(ev.get("peak_level", ev.get("avg_level", 0.0))),
                1 if ev.get("woke_me") else 0,
                1 if ev.get("tamper") else 0,
                1 if simulated else 0,
                now,
            ))
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO events (device_id, event_id, lat, lon, start_time, duration_ms, label,
                                    confidence, avg_level, peak_level, woke_me, tamper, simulated, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, event_id) DO UPDATE SET
                    lat=excluded.lat, lon=excluded.lon, start_time=excluded.start_time,
                    duration_ms=excluded.duration_ms, label=excluded.label,
                    confidence=excluded.confidence, avg_level=excluded.avg_level,
                    peak_level=excluded.peak_level, woke_me=excluded.woke_me,
                    tamper=excluded.tamper, received_at=excluded.received_at
                """,
                rows,
            )
            self._conn.commit()
        return len(rows)

    def query(self, hours: Optional[float]) -> Dict[str, Any]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if hours:
            cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            sql += " WHERE start_time >= ?"
            args = (cutoff,)
        sql += " ORDER BY start_time DESC LIMIT 20000"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            devs = self._conn.execute(
                """SELECT device_id, lat, lon, simulated, COUNT(*) AS n, MAX(received_at) AS last_seen
                   FROM events GROUP BY device_id"""
            ).fetchall()
        return {
            "events": [
                {
                    "device": r["device_id"], "id": r["event_id"], "lat": r["lat"], "lon": r["lon"],
                    "start": r["start_time"], "dur": r["duration_ms"], "label": r["label"],
                    "conf": round(r["confidence"], 2), "avg": round(r["avg_level"], 1),
                    "peak": round(r["peak_level"], 1), "woke": bool(r["woke_me"]),
                    "tamper": bool(r["tamper"]), "sim": bool(r["simulated"]),
                }
                for r in rows
            ],
            "devices": [
                {"device": d["device_id"], "lat": d["lat"], "lon": d["lon"], "sim": bool(d["simulated"]),
                 "events": d["n"], "last_seen": d["last_seen"]}
                for d in devs
            ],
        }

    def add_readings(self, payload: Dict[str, Any]) -> int:
        """Re-sending a payload for the same time window replaces it."""
        if payload.get("v") != 1:
            raise ValueError("only v: 1 payloads are accepted")
        station = str(payload["station_id"])[:64]
        readings = payload["readings"]
        if not isinstance(readings, list) or not readings:
            raise ValueError("readings must be a non-empty list")
        stamps = sorted(iso_utc(str(r["minute"])) for r in readings)
        with self._lock:
            old = self._conn.execute(
                "SELECT payload FROM readings WHERE station_id=? AND first_ts=?", (station, stamps[0])).fetchone()
            if old is not None:  # a re-send replaces the earlier copy in the summaries too
                op = json.loads(old["payload"])
                self._apply(station, None, None, _contributions(op), -1)
            self._apply(station, payload.get("lat"), payload.get("lon"), _contributions(payload), 1)
            self._conn.execute(
                """INSERT INTO readings (station_id, first_ts, last_ts, payload, received_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(station_id, first_ts) DO UPDATE SET
                       last_ts=excluded.last_ts, payload=excluded.payload, received_at=excluded.received_at""",
                (station, stamps[0], stamps[-1], json.dumps(payload), time.time()),
            )
            self._inserts += 1
            if self._inserts % 2000 == 0:
                self._prune()
            self._conn.commit()
        return len(readings)

    def summaries_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """v1 payloads with one reading per summary window: 5-minute windows from the
        start of the hour an hour ago, whole hours before that. Windows never overlap,
        and a window always comes back under the same timestamp, so the map can merge
        polls by timestamp like it does with raw readings."""
        now = datetime.now(timezone.utc)
        since_dt = (datetime.strptime(iso_utc(since), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    if since else now - timedelta(hours=24))
        split = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        queries = []
        if since_dt < split:  # whole hours that end after `since`
            queries.append((3600, (since_dt - timedelta(hours=1)).strftime(fmt), split.strftime(fmt), 3600))
        lo = max(since_dt, split) - timedelta(seconds=300)
        queries.append((300, lo.strftime(fmt), "9999", 300))
        stations: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for res, lo_ts, hi_ts, length in queries:
                rows = self._conn.execute(
                    "SELECT * FROM agg WHERE res=? AND start_ts>? AND start_ts<? ORDER BY start_ts",
                    (res, lo_ts, hi_ts)).fetchall()
                for r in rows:
                    end = datetime.strptime(r["start_ts"], fmt).replace(tzinfo=timezone.utc) + timedelta(seconds=length)
                    if end <= since_dt or r["valid_s"] <= 0 or r["e_sum"] <= 0:
                        continue
                    st = stations.setdefault(r["station_id"], {
                        "v": 1, "station_id": r["station_id"], "lat": r["lat"], "lon": r["lon"],
                        "summarised": True, "readings": []})
                    if r["lat"] is not None:
                        st["lat"], st["lon"] = r["lat"], r["lon"]
                    w = r["valid_s"]
                    src = json.loads(r["src"])
                    total = sum(src.values())
                    st["readings"].append({
                        "minute": r["start_ts"].replace(".000Z", "Z"),
                        "duration_s": round(r["duration_s"], 1),
                        "valid_s": round(w, 1),
                        "laeq_db": round(10 * math.log10(r["e_sum"] / w), 1),
                        "lmax_db": None if r["lmax"] is None else round(r["lmax"], 1),
                        "l90_db": round(r["l90_sum"] / w, 1),
                        "sources": {k: round(v / total, 3) for k, v in sorted(src.items(), key=lambda kv: -kv[1])}
                        if total > 0 else {},
                        "flags": sorted(json.loads(r["flags"])),
                        "n": r["n"],
                    })
        return list(stations.values())

    def readings_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        sql, args = "SELECT payload FROM readings", ()
        if since:
            sql += " WHERE last_ts > ?"
            args = (iso_utc(since),)
        # Newest first, so when there are more than the limit the map still gets current data.
        sql += " ORDER BY last_ts DESC LIMIT 5000"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def delete_simulated(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE simulated = 1")
            self._conn.commit()


# --- Simulated events, so /api/events has something to show without boxes ---
HOTSPOTS = [
    # name, d_lat, d_lon, radius_deg, boxes, label weights, active hours, base dB
    ("nightlife street", 0.006, -0.010, 0.004, 14, {"music": 5, "voices": 6, "traffic": 1}, range(21, 28), 62),
    ("ring road",       -0.012,  0.004, 0.009, 16, {"traffic": 9, "voices": 1},              range(0, 24),  60),
    ("construction site", 0.002, 0.016, 0.002,  6, {"construction": 8, "traffic": 2},         range(6, 18),  70),
    ("residential",     -0.004, -0.004, 0.012, 22, {"voices": 3, "background": 4, "traffic": 2, "music": 1}, range(0, 24), 50),
    ("tram depot",       0.014,  0.008, 0.002,  4, {"traffic": 6, "construction": 2},        range(0, 7),   64),
]


def seed_test_data(store: MapStore, center_lat: float, center_lon: float, days: int = 7, seed: int = 7) -> int:
    rng = random.Random(seed)
    now = datetime.now()
    total = 0
    box_n = 0
    for name, dlat, dlon, radius, boxes, weights, hours, base_db in HOTSPOTS:
        labels, w = zip(*weights.items())
        hours = list(hours)
        for _ in range(boxes):
            box_n += 1
            lat = center_lat + dlat + rng.gauss(0, radius / 2)
            lon = center_lon + dlon + rng.gauss(0, radius / 2) * 1.6
            loudness_bias = rng.uniform(-6, 6)
            events = []
            n_events = rng.randint(8, 45)
            for i in range(n_events):
                h = rng.choice(hours) % 24
                start = (now - timedelta(days=rng.randint(0, days - 1))).replace(
                    hour=h, minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0
                )
                if start > now:
                    start -= timedelta(days=1)
                label = rng.choices(labels, w)[0]
                avg = round(base_db + loudness_bias + rng.gauss(0, 4), 1)
                events.append({
                    "id": i + 1,
                    "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "duration_ms": int(rng.lognormvariate(8.3, 0.9)),
                    "label": label,
                    "confidence": round(rng.uniform(0.55, 0.97), 2),
                    "avg_level": avg,
                    "peak_level": round(avg + abs(rng.gauss(6, 3)), 1),
                    "woke_me": avg > 60 and rng.random() < 0.15,
                    "tamper": False,
                })
            total += store.upsert(f"sim-{name.split()[0]}-{box_n:02d}", lat, lon, events, simulated=True)
    return total


# --- HTTP ---
def make_handler(store: MapStore, quiet: bool = False):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # only uploads are logged, in do_POST

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            gz = len(body) > 4096 and "gzip" in self.headers.get("Accept-Encoding", "")
            if gz:
                body = gzip.compress(body, compresslevel=5)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/api/events":
                qs = parse_qs(url.query)
                try:
                    hours = float(qs.get("hours", ["0"])[0]) or None
                except ValueError:
                    hours = None
                self._json(200, store.query(hours))
                return
            if url.path == "/api/readings":
                qs = parse_qs(url.query)
                since = qs.get("since", [None])[0]
                try:
                    if qs.get("raw", ["0"])[0] in ("1", "true"):
                        self._json(200, {"payloads": store.readings_since(since)})
                    else:
                        self._json(200, {"payloads": store.summaries_since(since)})
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": f"bad since: {exc}"})
                return
            super().do_GET()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/events", "/api/readings"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                self._json(413 if length > MAX_BODY else 400, {"ok": False, "error": "bad body size"})
                return
            if path == "/api/readings":
                try:
                    body = json.loads(self.rfile.read(length))
                    n = store.add_readings(body)
                except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    return
                r = body["readings"][-1]
                if quiet:
                    self._json(200, {"ok": True, "stored": n})
                    return
                print(f"[Noise Map] analysis from {body['station_id']}: {n} reading(s), "
                      f"LAeq {r.get('laeq_db')} dB, sources {r.get('sources')} {r.get('flags') or ''}", flush=True)
                self._json(200, {"ok": True, "stored": n})
                return
            try:
                body = json.loads(self.rfile.read(length))
                device_id = str(body["device_id"])[:64]
                lat, lon = float(body["lat"]), float(body["lon"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("lat/lon out of range")
                events = body["events"] if "events" in body else [body]
                if not isinstance(events, list):
                    raise ValueError("events must be a list")
                n = store.upsert(device_id, lat, lon, events)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            print(f"[Noise Map] {n} event(s) from {device_id} @ {lat:.4f},{lon:.4f}", flush=True)
            self._json(200, {"ok": True, "stored": n})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Noise Map server for Night-Noise Box uploads")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(HERE / "noise_map.sqlite3"))
    ap.add_argument("--center", default="52.5200,13.4050", help="lat,lon to place simulated test boxes around")
    ap.add_argument("--no-seed", action="store_true", help="Don't generate test data on an empty DB")
    ap.add_argument("--reseed", action="store_true", help="Regenerate simulated test data")
    ap.add_argument("--raw-hours", type=float, default=0,
                    help="delete payloads older than this many hours (summaries stay %d days; 0 = keep all)" % AGG_KEEP_DAYS)
    ap.add_argument("--quiet", action="store_true", help="don't print a line per upload (for big mock fleets)")
    args = ap.parse_args()

    store = MapStore(args.db)
    store.raw_hours = args.raw_hours or None
    c_lat, c_lon = (float(x) for x in args.center.split(","))
    if args.reseed:
        store.delete_simulated()
    if args.reseed or (not args.no_seed and store.count() == 0):
        n = seed_test_data(store, c_lat, c_lon)
        print(f"[Noise Map] seeded {n} simulated events around {c_lat},{c_lon}")

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.request_queue_size = 256  # bursts of uploads from big fleets
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(store, args.quiet))
    print(f"[Noise Map] map at http://localhost:{args.port}, boxes upload to http://<this computer's IP>:{args.port}/api/readings")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
