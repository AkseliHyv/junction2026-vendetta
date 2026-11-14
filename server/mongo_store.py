"""MongoDB storage for the map server, used when MONGODB_URI is set.

Collections:
  readings  one document per reading. A time-series collection when the
            server supports it: `t` is the reading's start time and
            `station` ({id, lat, lon}) is the metadata.
  stations  one document per box with its last position as GeoJSON and a
            2dsphere index, so you can ask for boxes near a point.
  events    event records from events mode, keyed on (device, id).

Same methods as SqliteStore in server.py. Needs `pip install pymongo`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient, UpdateOne
from pymongo.errors import CollectionInvalid, OperationFailure

LABELS = ("traffic", "construction", "music", "voices", "background", "unknown")


def parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        self.stations.create_index([("location", GEOSPHERE)])
        self.events.create_index([("start_time", DESCENDING)])

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
        self.readings.insert_many(docs)

        update: Dict[str, Any] = {"$set": {"last_seen": datetime.now(timezone.utc)}}
        if "lat" in station:
            update["$set"]["location"] = {"type": "Point", "coordinates": [station["lon"], station["lat"]]}
        self.stations.update_one({"_id": station_id}, update, upsert=True)
        return len(docs)

    def readings_since(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """Readings newer than `since`, grouped into one payload per station."""
        query = {"t": {"$gt": parse_utc(since)}} if since else {}
        payloads: Dict[str, Dict[str, Any]] = {}
        for doc in self.readings.find(query, {"_id": 0}).sort("t", ASCENDING).limit(50_000):
            st = doc.pop("station", {})
            sid = st.get("id", "?")
            p = payloads.setdefault(sid, {"v": 1, "station_id": sid, "readings": []})
            if "lat" in st:
                p["lat"], p["lon"] = st["lat"], st["lon"]
            for k in ("model", "weighting", "calibration_offset_db"):
                if k in doc:
                    p[k] = doc.pop(k)
            doc["minute"] = iso_z(doc.pop("t"))
            p["readings"].append(doc)
        return list(payloads.values())

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
