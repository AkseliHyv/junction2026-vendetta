#!/usr/bin/env python3
"""Fills osm_cache/ from a downloaded .osm.pbf extract instead of the Overpass API.

    curl -LO https://download.geofabrik.de/europe/finland-latest.osm.pbf     # ~780 MB
    python3 -m venv /tmp/osm_venv && /tmp/osm_venv/bin/pip install osmium
    /tmp/osm_venv/bin/python mock-devices/city/osm_from_pbf.py finland-latest.osm.pbf

Writes the same files, in the same JSON shape, as osm_fetch.py would (Overpass
`out center` / `out geom`), cut to osm_fetch.BBOX. One pass over the file,
a few minutes. Data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import osmium

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from osm_fetch import BBOX, CACHE, ROADS  # noqa: E402

S, W, N, E = BBOX
ROAD_RE = re.compile(f"^({ROADS})$")
RAIL = {"rail", "tram", "subway", "light_rail"}
ROUTES = {"tram", "train", "subway", "light_rail"}
POI_AMENITY = {"bar", "pub", "nightclub", "school", "kindergarten", "ferry_terminal"}
GREEN = (("leisure", "park"), ("landuse", "forest"), ("natural", "wood"))
KEYS = ("building", "highway", "railway", "amenity", "landuse", "aeroway", "natural", "leisure", "route")


def inside(lat: float, lon: float) -> bool:
    return S <= lat <= N and W <= lon <= E


def geometry(way) -> Optional[List[Dict[str, float]]]:
    """Node locations of a way, or None when no node is inside the bbox."""
    nodes = way.nodes
    if not len(nodes):
        return None
    first = nodes[0].location  # cheap reject for the rest of Finland
    if first.valid() and not (S - 0.3 <= first.lat <= N + 0.3 and W - 0.6 <= first.lon <= E + 0.6):
        return None
    pts, hit = [], False
    for n in nodes:
        loc = n.location
        if not loc.valid():
            continue
        lat, lon = round(loc.lat, 7), round(loc.lon, 7)
        hit = hit or inside(lat, lon)
        pts.append({"lat": lat, "lon": lon})
    return pts if hit and len(pts) >= 2 else None


def center(pts: List[Dict[str, float]]) -> Dict[str, float]:
    """Bounding-box center, like Overpass `out center`."""
    la = [p["lat"] for p in pts]
    lo = [p["lon"] for p in pts]
    return {"lat": round((min(la) + max(la)) / 2, 7), "lon": round((min(lo) + max(lo)) / 2, 7)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pbf")
    ap.add_argument("--index", default="flex_mem",
                    help="node location index; in memory by default (~2 GB for Finland)")
    args = ap.parse_args()

    out: Dict[str, List[Dict[str, Any]]] = {k: [] for k in
                                           ("buildings_pbf", "roads_pbf", "rail_ways", "routes", "pois",
                                            "runways", "coastline", "green")}
    rail_geom: Dict[int, List[Dict[str, float]]] = {}  # every railway way, for the route relations
    t0, n_seen = time.monotonic(), 0

    fp = (osmium.FileProcessor(args.pbf)
          .with_locations(args.index)
          .with_filter(osmium.filter.KeyFilter(*KEYS)))
    for o in fp:
        n_seen += 1
        if n_seen % 1_000_000 == 0:
            print(f"  {n_seen / 1e6:.0f}M tagged objects, {time.monotonic() - t0:.0f}s", flush=True)
        tags = dict(o.tags)

        if o.is_node():
            lat, lon = o.location.lat, o.location.lon
            if inside(lat, lon) and (tags.get("amenity") in POI_AMENITY or tags.get("landuse") == "construction"):
                out["pois"].append({"type": "node", "id": o.id, "lat": round(lat, 7), "lon": round(lon, 7),
                                    "tags": tags})
            continue

        if o.is_way():
            g = geometry(o)
            if g is None:
                continue
            if "building" in tags:
                out["buildings_pbf"].append({"type": "way", "id": o.id, "center": center(g), "tags": tags})
            if ROAD_RE.match(tags.get("highway", "")):
                out["roads_pbf"].append({"type": "way", "id": o.id, "tags": tags, "geometry": g})
            if "railway" in tags:
                rail_geom[o.id] = g
                if tags["railway"] in RAIL:
                    out["rail_ways"].append({"type": "way", "id": o.id, "tags": tags})
            if tags.get("amenity") in POI_AMENITY or tags.get("landuse") == "construction":
                out["pois"].append({"type": "way", "id": o.id, "center": center(g), "tags": tags})
            if tags.get("aeroway") == "runway":
                out["runways"].append({"type": "way", "id": o.id, "tags": tags, "geometry": g})
            if tags.get("natural") == "coastline":
                out["coastline"].append({"type": "way", "id": o.id, "geometry": g})
            if any(tags.get(k) == v for k, v in GREEN):
                out["green"].append({"type": "way", "id": o.id, "center": center(g)})
            continue

        # Relations come last in a .pbf, so every railway way is known by now.
        if tags.get("route") in ROUTES:
            members, hit = [], False
            for m in o.members:
                if m.type != "w":
                    continue
                mm: Dict[str, Any] = {"type": "way", "ref": m.ref, "role": m.role}
                g = rail_geom.get(m.ref)
                if g:
                    # Like Overpass `geom(bbox)`: points outside the bbox are null.
                    mm["geometry"] = [p if inside(p["lat"], p["lon"]) else None for p in g]
                    hit = True
                members.append(mm)
            if hit:
                out["routes"].append({"type": "relation", "id": o.id, "tags": tags, "members": members})

    print(f"  read in {time.monotonic() - t0:.0f}s")
    CACHE.mkdir(exist_ok=True)
    old = [p for p in CACHE.glob("*.json")]
    if old:  # earlier Overpass downloads would be read twice
        keep = CACHE / "overpass"
        keep.mkdir(exist_ok=True)
        for p in old:
            shutil.move(str(p), keep / p.name)
        print(f"  moved {len(old)} earlier files to {keep}")
    for name, elements in out.items():
        path = CACHE / f"{name}.json"
        path.write_text(json.dumps({"elements": elements}, ensure_ascii=False, separators=(",", ":")))
        print(f"  {name}: {len(elements)} elements, {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
