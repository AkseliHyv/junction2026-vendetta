#!/usr/bin/env python3
"""Downloads the OpenStreetMap data build_city.py needs, into osm_cache/.

    python3 mock-devices/city/osm_fetch.py            # whole capital region
    python3 mock-devices/city/osm_fetch.py --force    # download again

Uses the public Overpass API, one query at a time. When its servers are busy
(the building tiles can take hours of retries), use osm_from_pbf.py with a
Geofabrik extract instead. Only needed to rebuild the city; the built devices
are committed. Data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent
CACHE = HERE / "osm_cache"
# Public Overpass servers, tried in turn when one is busy.
OVERPASS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "noise-map-mock-city/0.1 (python urllib)"

# Helsinki, Espoo, Vantaa and Kauniainen.
BBOX = (60.10, 24.50, 60.37, 25.27)  # south, west, north, east

ROADS = "motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street"


def tiles(bbox: Tuple[float, float, float, float], ny: int, nx: int) -> List[Tuple[float, float, float, float]]:
    s, w, n, e = bbox
    dy, dx = (n - s) / ny, (e - w) / nx
    return [(round(s + i * dy, 5), round(w + j * dx, 5), round(s + (i + 1) * dy, 5), round(w + (j + 1) * dx, 5))
            for i in range(ny) for j in range(nx)]


def bb(b: Tuple[float, float, float, float]) -> str:
    return ",".join(str(x) for x in b)


def queries() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, t in enumerate(tiles(BBOX, 4, 4)):
        out.append((f"buildings_{k:02d}", f"""
            way["building"]({bb(t)});
            out center tags;"""))
    for k, t in enumerate(tiles(BBOX, 2, 2)):
        out.append((f"roads_{k}", f"""
            way["highway"~"^({ROADS})$"]({bb(t)});
            out tags geom;"""))
    b = bb(BBOX)
    out += [
        ("rail_ways", f"""
            way["railway"~"^(rail|tram|subway|light_rail)$"]({b});
            out tags;"""),
        ("routes", f"""
            rel["route"~"^(tram|train|subway|light_rail)$"]({b});
            out tags geom({b});"""),
        ("pois", f"""
            (nwr["amenity"~"^(bar|pub|nightclub|school|kindergarten|ferry_terminal)$"]({b});
             nwr["landuse"="construction"]({b}););
            out center tags;"""),
        ("runways", f"""way["aeroway"="runway"]({b}); out tags geom;"""),
        ("coastline", f"""way["natural"="coastline"]({b}); out geom;"""),
        ("green", f"""
            (way["leisure"="park"]({b}); way["landuse"="forest"]({b}); way["natural"="wood"]({b}););
            out center;"""),
    ]
    return out


def fetch(name: str, q: str, force: bool) -> None:
    path = CACHE / f"{name}.json"
    if path.exists() and not force:
        print(f"  {name}: cached")
        return
    body = urllib.parse.urlencode({"data": f"[out:json][timeout:300][maxsize:1073741824];{q}"}).encode()
    for attempt in range(8):
        t0 = time.monotonic()
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=420) as resp:
                data = resp.read()
            n = len(json.loads(data).get("elements", []))
            path.write_bytes(data)
            print(f"  {name}: {n} elements, {len(data) / 1e6:.1f} MB, {time.monotonic() - t0:.0f}s", flush=True)
            time.sleep(2)  # be polite to the public server
            return
        except Exception as exc:
            wait = 10 * (attempt + 1)
            print(f"  {name}: {url.split('/')[2]}: {type(exc).__name__}: {exc}; retry in {wait}s", flush=True)
            time.sleep(wait)
    sys.exit(f"giving up on {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", help="comma-separated query names")
    args = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    wanted = set(args.only.split(",")) if args.only else None
    for name, q in queries():
        if wanted is None or name in wanted or name.split("_")[0] in wanted:
            fetch(name, q, args.force)


if __name__ == "__main__":
    main()
