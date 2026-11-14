"""MongoDB storage for the map server, used when MONGODB_URI is set.

Collections:
  readings  one document per reading. A time-series collection when the
            server supports it: `t` is the reading's start time and
            `station` ({id, lat, lon}) is the metadata. Write-only: kept for
            querying in Atlas, the map reads `payloads` and `agg`.
  payloads  each analysis payload as the box sent it, keyed on (station,
            first reading), so a re-send replaces the earlier copy.
            GET /api/readings?raw=1 reads these. --raw-hours prunes them.
  agg       per-station 5-minute and hourly summaries (see summaries.py),
            what GET /api/readings returns. Kept AGG_KEEP_DAYS days.
  stations  one document per box with its last position as GeoJSON and a
            2dsphere index, so you can ask for boxes near a point.
  events    event records from events mode, keyed on (device, id).

Same methods as SqliteStore in server.py. Needs `pip install pymongo`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient, UpdateOne
from pymongo.errors import CollectionInvalid, OperationFailure

from summaries import AGG_KEEP_DAYS, TS_FMT, contributions, iso_utc, summary_payloads, summary_plan

LABELS = ("traffic", "construction", "music", "voices", "background", "unknown")


def parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _field(key: str) -> str:
    """A source label or flag as a safe field name (no dots, no leading $)."""
    return str(key).replace(".", "_").lstrip("$") or "_"


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:  # pymongo returns naive UTC datetimes
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MongoStore:
    def __init__(self, uri: str, db_name: str = "noise_map") -> None:
        self._client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="noise-map")
        self._client.admin.command("ping")  # fail at startup, not on the first upload
        db = self._client[db_name]
        self._create_readings(db)
        self.readings = db["readings"]
        self.stations = db["stations"]
        self.events = db["events"]
        self.payloads = db["payloads"]
        self.agg = db["agg"]
        self.stations.create_index([("location", GEOSPHERE)])
        self.events.create_index([("start_time", DESCENDING)])
        self.payloads.create_index([("last_ts", DESCENDING)])
        self.agg.create_index([("res", ASCENDING), ("start_ts", ASCENDING)])
        self.raw_hours: Optional[float] = None
        self._inserts = 0
        if self.agg.estimated_document_count() == 0:
            self._rebuild_agg()

    @staticmethod
    def _create_readings(db: Any) -> None:
        if "readings" in db.list_collection_names():
            return
        try:
            db.create_collection(
                "readings", timeseries={"timeField": "t", "metaField": "station", "granularity": "seconds"}
            )
        except (CollectionInvalid, OperationFailure) as exc:
            print(f"[Noise Map] time-series collection not available ({exc}); using a normal one")
            db.create_collection("readings")
            db["readings"].create_index([("t", ASCENDING)])

    # --- Readings (analysis mode) ---

    def add_readings(self, payload: Dict[str, Any]) -> int:
        if payload.get("v") != 1:
            raise ValueError("only v: 1 payloads are accepted")
        station_id = str(payload["station_id"])[:64]
        readings = payload["readings"]
        if not isinstance(readings, list) or not readings:
            raise ValueError("readings must be a non-empty list")
        lat, lon = payload.get("lat"), payload.get("lon")
        station: Dict[str, Any] = {"id": station_id}
        if lat is not None and lon is not None:
            lat, lon = float(lat), float(lon)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat/lon out of range")
            station.update(lat=lat, lon=lon)

        extra = {k: payload[k] for k in ("model", "weighting", "calibration_offset_db") if k in payload}
        docs = []
        for r in readings:
            doc = {k: v for k, v in r.items() if k != "minute"}
            doc.update(extra, t=parse_utc(r["minute"]), station=station)
            docs.append(doc)
        stamps = sorted(iso_utc(str(r["minute"])) for r in readings)

        old = self.payloads.find_one_and_replace(
            {"_id": f"{station_id}|{stamps[0]}"},
            {"station_id": station_id, "first_ts": stamps[0], "last_ts": stamps[-1],
             "payload": json.dumps(payload), "received_at": datetime.now(timezone.utc)},
            upsert=True,
        )
        ops = []
        if old is not None:  # a re-send replaces the earlier copy in the summaries too
            ops += self._agg_ops(station_id, None, None, contributions(json.loads(old["payload"])), -1)
        ops += self._agg_ops(station_id, station.get("lat"), station.get("lon"), contributions(payload), 1)
        if ops:
            self.agg.bulk_write(ops, ordered=True)
        if old is None:
            self.readings.insert_many(docs)
        self._inserts += 1
        if self._inserts % 2000 == 0:
            self._prune()

        update: Dict[str, Any] = {"$set": {"last_seen": datetime.now(timezone.utc)}}
        if "lat" in station:
            update["$set"]["location"] = {"type": "Point", "coordinates": [station["lon"], station["lat"]]}
        self.stations.update_one({"_id": station_id}, update, upsert=True)
        return len(docs)

    def readings_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """Payloads as the boxes sent them, newer than `since`. Newest first, so when
        there are more than the limit the map still gets current data."""
        query = {"last_ts": {"$gt": iso_utc(since)}} if since else {}
        docs = list(self.payloads.find(query, {"payload": 1}).sort("last_ts", DESCENDING).limit(5000))
        return [json.loads(d["payload"]) for d in reversed(docs)]

    def summaries_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """Summarised payloads for the map; see summaries.summary_plan()."""
        since_dt, plan = summary_plan(since)
        rows: List[Dict[str, Any]] = []
        for res, lo_ts, hi_ts in plan:
            rows += self.agg.find({"res": res, "start_ts": {"$gt": lo_ts, "$lt": hi_ts}}, {"_id": 0}).sort(
                "start_ts", ASCENDING)
        return summary_payloads(rows, since_dt)

    @staticmethod
    def _agg_ops(station: str, lat: Any, lon: Any, contrib: Dict[Any, Dict[str, Any]], sign: int) -> List[UpdateOne]:
        """Add (sign=1) or take back (sign=-1) a payload's contribution, as atomic updates."""
        ops = []
        for (res, start), c in contrib.items():
            inc = {k: sign * c[k] for k in ("duration_s", "valid_s", "e_sum", "l90_sum", "n")}
            inc.update({f"src.{_field(k)}": sign * v for k, v in c["src"].items()})
            inc.update({f"flags.{_field(k)}": sign * v for k, v in c["flags"].items()})
            update: Dict[str, Any] = {"$inc": inc, "$setOnInsert": {"res": res, "station_id": station, "start_ts": start}}
            if sign > 0 and c["lmax"] is not None:
                update["$max"] = {"lmax": c["lmax"]}
            if sign > 0 and lat is not None:
                update["$set"] = {"lat": lat, "lon": lon}
            ops.append(UpdateOne({"_id": f"{res}|{station}|{start}"}, update, upsert=sign > 0))
        return ops

    def _rebuild_agg(self) -> None:
        """Summaries for readings stored before the agg collection existed."""
        ops: List[UpdateOne] = []
        n = 0
        for doc in self.readings.find({}, {"_id": 0}).sort("t", ASCENDING):
            st = doc.pop("station", {})
            doc["minute"] = iso_z(doc.pop("t"))
            ops += self._agg_ops(str(st.get("id", "?")), st.get("lat"), st.get("lon"),
                                 contributions({"readings": [doc]}), 1)
            n += 1
            if len(ops) >= 1000:
                self.agg.bulk_write(ops, ordered=False)
                ops = []
        if ops:
            self.agg.bulk_write(ops, ordered=False)
        if n:
            print(f"[Noise Map] summarised {n} stored readings")

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        self.agg.delete_many({"start_ts": {"$lt": (now - timedelta(days=AGG_KEEP_DAYS)).strftime(TS_FMT)}})
        if self.raw_hours:
            self.payloads.delete_many({"last_ts": {"$lt": (now - timedelta(hours=self.raw_hours)).strftime(TS_FMT)}})

    # --- Events (events mode) ---

    def upsert(self, device_id: str, lat: float, lon: float, events: List[Dict[str, Any]], simulated: bool = False) -> int:
        now = datetime.now(timezone.utc)
        ops = []
        for ev in events:
            label = str(ev.get("label", "unknown"))
            doc = {
                "device": device_id,
                "lat": float(ev.get("lat", lat)),
                "lon": float(ev.get("lon", lon)),
                "start_time": str(ev["start_time"]),
                "duration_ms": max(0, int(ev.get("duration_ms", 0))),
                "label": label if label in LABELS else "unknown",
                "confidence": float(ev.get("confidence", 0.0)),
                "avg_level": float(ev.get("avg_level", 0.0)),
                "peak_level": float(ev.get("peak_level", ev.get("avg_level", 0.0))),
                "woke_me": bool(ev.get("woke_me")),
                "tamper": bool(ev.get("tamper")),
                "simulated": simulated,
                "received_at": now,
            }
            ops.append(UpdateOne({"_id": {"device": device_id, "id": int(ev["id"])}}, {"$set": doc}, upsert=True))
        if ops:
            self.events.bulk_write(ops, ordered=False)
        return len(ops)

    def query(self, hours: Optional[float]) -> Dict[str, Any]:
        query: Dict[str, Any] = {}
        if hours:
            cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            query["start_time"] = {"$gte": cutoff}
        rows = self.events.find(query).sort("start_time", DESCENDING).limit(20_000)
        devices = self.events.aggregate([
            {"$group": {
                "_id": "$device", "lat": {"$last": "$lat"}, "lon": {"$last": "$lon"},
                "sim": {"$max": "$simulated"}, "events": {"$sum": 1}, "last_seen": {"$max": "$received_at"},
            }},
        ])
        return {
            "events": [
                {
                    "device": r["device"], "id": r["_id"]["id"], "lat": r["lat"], "lon": r["lon"],
                    "start": r["start_time"], "dur": r["duration_ms"], "label": r["label"],
                    "conf": round(r["confidence"], 2), "avg": round(r["avg_level"], 1),
                    "peak": round(r["peak_level"], 1), "woke": r["woke_me"], "tamper": r["tamper"],
                    "sim": r["simulated"],
                }
                for r in rows
            ],
            "devices": [
                {"device": d["_id"], "lat": d["lat"], "lon": d["lon"], "sim": bool(d["sim"]),
                 "events": d["events"], "last_seen": d["last_seen"].replace(tzinfo=timezone.utc).timestamp()}
                for d in devices
            ],
        }

    def count(self) -> int:
        return self.events.count_documents({})

    def delete_simulated(self) -> None:
        self.events.delete_many({"simulated": True})
