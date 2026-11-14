"""Per-station summary windows for GET /api/readings, shared by SqliteStore
(server.py) and MongoStore (mongo_store.py).

Every reading adds to one 5-minute and one hourly window of its station. A
window holds sums weighted by valid_s, so readings of any length combine like
the map does it (energy average for LAeq):

    duration_s, valid_s, n            plain sums
    e_sum                             sum of valid_s * 10^(laeq/10)
    l90_sum                           sum of valid_s * l90
    lmax                              max
    src    {label: sum of valid_s * score}
    flags  {flag: count}

A store keeps these per (res, station_id, start_ts) and adds or takes back a
payload's contributions(). summary_plan() says which windows to read for a
`since`, and summary_payloads() turns them into v1 payloads for the map.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

AGG_RES = (300, 3600)  # summary windows kept per station: 5 minutes and 1 hour
AGG_KEEP_DAYS = 8
TS_FMT = "%Y-%m-%dT%H:%M:%S.000Z"


def iso_utc(ts: str) -> str:
    """Any ISO timestamp as UTC "YYYY-MM-DDTHH:MM:SS.mmmZ", so strings sort by time."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def window_start(ts: str, res: int) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    t = int(dt.timestamp()) // res * res
    return datetime.fromtimestamp(t, timezone.utc).strftime(TS_FMT)


def contributions(payload: Dict[str, Any]) -> Dict[Tuple[int, str], Dict[str, Any]]:
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
            c = out.setdefault((res, window_start(ts, res)), {
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


def summary_plan(since: Optional[str]) -> Tuple[datetime, List[Tuple[int, str, str]]]:
    """The windows to read for `since`: 5-minute windows from the start of the hour an
    hour ago, whole hours before that. Windows never overlap, and a window always comes
    back under the same timestamp, so the map can merge polls by timestamp like it does
    with raw readings. Returns (since, [(res, start_ts after, start_ts before)])."""
    now = datetime.now(timezone.utc)
    since_dt = (datetime.strptime(iso_utc(since), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                if since else now - timedelta(hours=24))
    split = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    queries = []
    if since_dt < split:  # whole hours that end after `since`
        queries.append((3600, (since_dt - timedelta(hours=1)).strftime(TS_FMT), split.strftime(TS_FMT)))
    lo = max(since_dt, split) - timedelta(seconds=300)
    queries.append((300, lo.strftime(TS_FMT), "9999"))
    return since_dt, queries


def summary_payloads(rows: Iterable[Dict[str, Any]], since_dt: datetime) -> List[Dict[str, Any]]:
    """Window rows (sorted by start_ts) as one v1 payload per station, one reading per window.
    A row has res, station_id, start_ts, lat, lon, the sums above, and src/flags as dicts."""
    stations: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        end = datetime.strptime(r["start_ts"], TS_FMT).replace(tzinfo=timezone.utc) + timedelta(seconds=r["res"])
        w = r["valid_s"]
        if end <= since_dt or w <= 0 or r["e_sum"] <= 0:
            continue
        st = stations.setdefault(r["station_id"], {
            "v": 1, "station_id": r["station_id"], "lat": r.get("lat"), "lon": r.get("lon"),
            "summarised": True, "readings": []})
        if r.get("lat") is not None:
            st["lat"], st["lon"] = r["lat"], r["lon"]
        src = {k: v for k, v in (r.get("src") or {}).items() if v > 1e-6}  # 1e-6: float residue after a take-back
        total = sum(src.values())
        lmax = r.get("lmax")
        st["readings"].append({
            "minute": r["start_ts"].replace(".000Z", "Z"),
            "duration_s": round(r["duration_s"], 1),
            "valid_s": round(w, 1),
            "laeq_db": round(10 * math.log10(r["e_sum"] / w), 1),
            "lmax_db": None if lmax is None else round(lmax, 1),
            "l90_db": round(r["l90_sum"] / w, 1),
            "sources": {k: round(v / total, 3) for k, v in sorted(src.items(), key=lambda kv: -kv[1])}
            if total > 0 else {},
            "flags": sorted(k for k, v in (r.get("flags") or {}).items() if v > 0),
            "n": int(r["n"]),
        })
    return list(stations.values())
