#!/usr/bin/env python3
"""
Night-Noise Box — Noise Map Server (server/server.py)
-----------------------------------------------------
Receives JSON event uploads from one or more UNO Q boxes
(`python/uploader.py`) and serves a real street map with a noise heatmap on
top (`server/static/index.html`). Click anywhere on the heat to see which
noises were recorded there.

    python3 server/server.py                 # http://localhost:8080, seeds test data if DB is empty
    python3 server/server.py --no-seed       # start empty (real boxes only)
    python3 server/server.py --reseed        # wipe simulated rows and regenerate them
    python3 server/server.py --center 60.17,24.94   # seed test data around another city

API:
    POST /api/events    {"device_id", "lat", "lon", "events": [record, ...]}
                        (a single record with device_id/lat/lon inline also works)
    GET  /api/events    ?hours=N  -> {"events": [...], "devices": [...]}
    POST /api/readings  v1 analysis payload from python/analysis.py, stored unchanged
    GET  /api/readings  ?since=<ISO> -> {"payloads": [...]}  (contract in static/README.md;
                        open the map with ?api=/api to use it instead of the demo feed)

Standard library only. Records are upserted on (device_id, event id), so a box
re-sending an event after a "woke me" press updates it instead of duplicating.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
LABELS = ("traffic", "construction", "music", "voices", "background", "unknown")
MAX_BODY = 1_000_000

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
"""


def iso_utc(ts: str) -> str:
    """Normalises an ISO-8601 timestamp to "YYYY-MM-DDTHH:MM:SS.mmmZ" (UTC) so text comparison sorts by time."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MapStore:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

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
        """Stores one v1 analysis payload unchanged; re-sending the same window replaces it."""
        if payload.get("v") != 1:
            raise ValueError("only v: 1 payloads are accepted")
        station = str(payload["station_id"])[:64]
        readings = payload["readings"]
        if not isinstance(readings, list) or not readings:
            raise ValueError("readings must be a non-empty list")
        stamps = sorted(iso_utc(str(r["minute"])) for r in readings)
        with self._lock:
            self._conn.execute(
                """INSERT INTO readings (station_id, first_ts, last_ts, payload, received_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(station_id, first_ts) DO UPDATE SET
                       last_ts=excluded.last_ts, payload=excluded.payload, received_at=excluded.received_at""",
                (station, stamps[0], stamps[-1], json.dumps(payload), time.time()),
            )
            self._conn.commit()
        return len(readings)

    def readings_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        sql, args = "SELECT payload FROM readings", ()
        if since:
            sql += " WHERE last_ts > ?"
            args = (iso_utc(since),)
        sql += " ORDER BY last_ts LIMIT 5000"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def delete_simulated(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE simulated = 1")
            self._conn.commit()


# ---------------------------------------------------------------------------
# Test data: simulated boxes around a few plausible noise hotspots
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_handler(store: MapStore):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # uploads are logged in do_POST; static/GET traffic stays quiet

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
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
                since = parse_qs(url.query).get("since", [None])[0]
                try:
                    self._json(200, {"payloads": store.readings_since(since)})
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
    args = ap.parse_args()

    store = MapStore(args.db)
    c_lat, c_lon = (float(x) for x in args.center.split(","))
    if args.reseed:
        store.delete_simulated()
    if args.reseed or (not args.no_seed and store.count() == 0):
        n = seed_test_data(store, c_lat, c_lon)
        print(f"[Noise Map] seeded {n} simulated events around {c_lat},{c_lon}")

    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"[Noise Map] map at http://localhost:{args.port}  ·  boxes POST to http://<this-host>:{args.port}/api/events")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
