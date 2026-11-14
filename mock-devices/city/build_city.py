#!/usr/bin/env python3
"""Places mock boxes on real buildings and works out what each one hears.

    python3 mock-devices/city/osm_fetch.py      # once, downloads osm_cache/
    python3 mock-devices/city/build_city.py     # writes devices.json and emitters.json

Boxes go where people would put a night-noise box: mostly on homes (weighted
by how many people a building holds), some on hotspots (main roads, bar
streets, tram lines). For each box the builder records:

  * road segments nearby, with the distance, where along the road the box is
    (so a car passes boxes on the same street one after another), how much of
    the road the box "sees", and how much buildings screen it;
  * tram, train and metro lines (from route relations; tunnels are silent);
  * bars, schools, daycares, building sites, venues and ferry terminals;
  * flight paths of Helsinki-Vantaa's runways;
  * a background level from all the more distant traffic;
  * wind exposure (shore, greenery, floor).

At run time city/world.py turns these into sound. Standard library only.
Map data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
CACHE = HERE / "osm_cache"

LAT0, LON0 = 60.20, 24.90
KX = 111_320.0 * math.cos(math.radians(LAT0))
KY = 110_574.0


def xy(lat: float, lon: float) -> Tuple[float, float]:
    return (lon - LON0) * KX, (lat - LAT0) * KY


def latlon(x: float, y: float) -> Tuple[float, float]:
    return LAT0 + y / KY, LON0 + x / KX


# --- Road model ---------------------------------------------------------------
# Per-direction peak-hour flow (vehicles/h), default speed (km/h), heavy share,
# buses/h per direction, and how far away a box still hears single vehicles (m).
ROAD = {
    "motorway":       (2100, 100, 0.08, 4, 300),
    "trunk":          (1300, 80, 0.07, 8, 300),
    "primary":        (800, 50, 0.05, 18, 250),
    "secondary":      (450, 40, 0.04, 10, 200),
    "tertiary":       (220, 40, 0.03, 5, 150),
    "unclassified":   (60, 40, 0.03, 0.5, 90),
    "residential":    (30, 30, 0.01, 0.3, 80),
    "living_street":  (8, 20, 0.01, 0, 40),
    "motorway_link":  (500, 60, 0.07, 1, 150),
    "trunk_link":     (350, 50, 0.06, 2, 150),
    "primary_link":   (300, 40, 0.05, 4, 120),
    "secondary_link": (150, 40, 0.04, 2, 100),
    "tertiary_link":  (80, 30, 0.03, 1, 80),
}
FAR_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link", "primary_link"}


def car_lref(v_kmh: float) -> float:
    """Pass-by peak at 7.5 m (dB, Z-weighted-ish)."""
    return 70.0 + 30.0 * math.log10(max(v_kmh, 30.0) / 50.0)


def mix_lref(v_kmh: float, heavy: float) -> float:
    lc = car_lref(v_kmh)
    return 10 * math.log10((1 - heavy) * 10 ** (lc / 10) + heavy * 10 ** ((lc + 9) / 10))


def parse_speed(s: Optional[str], default: float) -> float:
    if s:
        m = re.match(r"\d+", s)
        if m:
            v = float(m.group())
            return v * 1.609 if "mph" in s else v
    return default


# Rail: mode -> (pass speed m/s, travel speed m/s incl. stops, vehicle length m, Lref at 7.5 m)
RAIL = {
    "tram": (7.0, 4.5, 30, 79),
    "light_rail": (9.0, 6.5, 45, 78),
    "subway": (15.0, 11.0, 90, 83),
    "train": (20.0, 16.0, 110, 86),
}

# Hand-placed sites with schedules that OSM doesn't have.
VENUES = [
    # id, lat, lon, capacity weight, name
    ("venue-olympic-stadium", 60.18690, 24.92700, 3.0, "Olympic Stadium"),
    ("venue-veikkaus-arena", 60.20550, 24.92860, 2.0, "Veikkaus Arena"),
    ("venue-helsinki-ice-hall", 60.18800, 24.92160, 1.2, "Helsinki Ice Hall"),
    ("venue-bolt-arena", 60.18800, 24.92500, 1.0, "Bolt Arena"),
    ("venue-messukeskus", 60.20330, 24.93700, 1.5, "Messukeskus"),
    ("venue-metro-areena", 60.17800, 24.78430, 1.0, "Metro Areena, Espoo"),
    ("venue-hype-areena", 60.15675, 24.63008, 0.8, "Hype Areena, Espoo"),
]
FERRIES = [
    # id, lat, lon, kind
    ("ferry-west-harbour", 60.14950, 24.91400, "ferry_major"),
    ("ferry-katajanokka", 60.16650, 24.96650, "ferry_major"),
    ("ferry-olympia", 60.16100, 24.95600, "ferry_major"),
    ("ferry-vuosaari-harbour", 60.20700, 25.19500, "ferry_cargo"),
    ("ferry-suomenlinna-market", 60.16750, 24.95350, "ferry_small"),
    ("ferry-suomenlinna-pier", 60.14750, 24.98450, "ferry_small"),
]
POINT_RANGE = {
    "bar": 140, "pub": 120, "nightclub": 200, "school": 220, "kindergarten": 160, "construction": 400,
    "venue": 900, "ferry_major": 1500, "ferry_cargo": 1200, "ferry_small": 500,
}

# Building types that are homes, with default levels and a weight per level.
HOMES = {
    "apartments": (5, 1.0), "residential": (3, 0.8), "dormitory": (4, 0.8), "terrace": (2, 0.6),
    "terraced": (2, 0.6), "house": (1.5, 0.5), "detached": (1.5, 0.5), "semidetached_house": (2, 0.5),
}
# Hotspot boxes also go on untyped and mixed-use buildings (flats above shops and bars), not on sheds.
HOTSPOT_BUILDINGS = {"yes", "commercial", "retail", "mixed_use", "hotel"}


# --- Geometry helpers ---------------------------------------------------------

class Grid:
    """Buckets items by cell for neighbourhood queries."""

    def __init__(self, cell: float) -> None:
        self.cell = cell
        self.cells: Dict[Tuple[int, int], List[Any]] = defaultdict(list)

    def add_point(self, x: float, y: float, item: Any) -> None:
        self.cells[(int(x // self.cell), int(y // self.cell))].append(item)

    def add_segment(self, x1: float, y1: float, x2: float, y2: float, item: Any) -> None:
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) // (self.cell / 2)) + 1)
        seen = set()
        for i in range(n + 1):
            t = i / n
            c = (int((x1 + (x2 - x1) * t) // self.cell), int((y1 + (y2 - y1) * t) // self.cell))
            if c not in seen:
                seen.add(c)
                self.cells[c].append(item)

    def near(self, x: float, y: float, r: float) -> Iterable[Any]:
        c = self.cell
        for i in range(int((x - r) // c), int((x + r) // c) + 1):
            for j in range(int((y - r) // c), int((y + r) // c) + 1):
                yield from self.cells.get((i, j), ())


def seg_closest(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> Tuple[float, float, float, float]:
    """(distance, t along AB 0..1, closest x, closest y)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t, cx, cy


def seg_view(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """theta / d_perp for a line-source segment: its share of energy, in 1/m."""
    ux, uy, vx, vy = ax - px, ay - py, bx - px, by - py
    theta = abs(math.atan2(ux * vy - uy * vx, ux * vx + uy * vy))
    L = math.hypot(bx - ax, by - ay)
    if L == 0:
        return 0.0
    dperp = abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / L
    return theta / max(dperp, 3.0)


class Polyline:
    def __init__(self, pts: Sequence[Tuple[float, float]], silent: Optional[Sequence[bool]] = None) -> None:
        self.pts = list(pts)
        self.silent = list(silent) if silent else [False] * (len(pts) - 1)
        self.cum = [0.0]
        for (x1, y1), (x2, y2) in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x2 - x1, y2 - y1))

    @property
    def length(self) -> float:
        return self.cum[-1]

    def closest(self, px: float, py: float, segs: Optional[Iterable[int]] = None):
        """(distance, s along, cx, cy, segment index)."""
        best = (math.inf, 0.0, 0.0, 0.0, -1)
        for i in (segs if segs is not None else range(len(self.pts) - 1)):
            (ax, ay), (bx, by) = self.pts[i], self.pts[i + 1]
            d, t, cx, cy = seg_closest(px, py, ax, ay, bx, by)
            if d < best[0]:
                best = (d, self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), cx, cy, i)
        return best

    def view(self, px: float, py: float, r: float) -> float:
        v = 0.0
        for i, ((ax, ay), (bx, by)) in enumerate(zip(self.pts, self.pts[1:])):
            if self.silent[i]:
                continue
            if seg_closest(px, py, ax, ay, bx, by)[0] <= r:
                v += seg_view(px, py, ax, ay, bx, by)
        return v


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:28] or "x"


def point_of(e: Dict[str, Any]) -> Tuple[float, float]:
    """(lat, lon) of an element from `out center` or from a `convert` with a Point geometry."""
    if "center" in e:
        return e["center"]["lat"], e["center"]["lon"]
    if "geometry" in e and isinstance(e["geometry"], dict):
        lon, lat = e["geometry"]["coordinates"]
        return lat, lon
    return e["lat"], e["lon"]


def line_of(e: Dict[str, Any]) -> List[Tuple[float, float]]:
    """[(lat, lon), ...] of a way from `out geom` or from a `convert` with a LineString."""
    g = e.get("geometry")
    if isinstance(g, dict):
        return [(lat, lon) for lon, lat in g["coordinates"]]
    return [(p["lat"], p["lon"]) for p in g or [] if p]


def load(name: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in sorted(CACHE.glob(f"{name}*.json")):
        out += json.loads(p.read_text())["elements"]
    if not out:
        raise SystemExit(f"no {name} data in {CACHE}; run osm_fetch.py first")
    return out


# --- Builder ------------------------------------------------------------------

class City:
    def __init__(self) -> None:
        t0 = time.monotonic()
        self.buildings: List[Tuple[int, float, float, str, Optional[float]]] = []
        self.bgrid = Grid(20.0)  # building raster for screening
        seen = set()
        for e in load("buildings_"):
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            lat, lon = point_of(e)
            x, y = xy(lat, lon)
            lv = e["tags"].get("levels") or e["tags"].get("building:levels") or ""
            m = re.match(r"\d+(\.\d+)?", lv)
            self.buildings.append((e["id"], x, y, e["tags"].get("building", "yes"), float(m.group()) if m else None))
            self.bgrid.add_point(x, y, len(self.buildings) - 1)
        print(f"  {len(self.buildings)} buildings")

        self.roads: Dict[str, Dict[str, Any]] = {}
        self.road_lines: Dict[str, Polyline] = {}
        self.rgrid = Grid(100.0)
        for e in load("roads_"):
            t = e["tags"]
            rid = f"w{e['id']}"
            if rid in self.roads or t.get("tunnel") in ("yes", "building_passage") or t.get("covered") == "yes":
                continue
            coords = line_of(e)
            if len(coords) < 2:
                continue
            cls = t["highway"]
            flow, v0, heavy, bus, _ = ROAD[cls]
            pts = [xy(lat, lon) for lat, lon in coords]
            pl = Polyline(pts)
            oneway = t.get("oneway") in ("yes", "1", "true") or cls.startswith("motorway")
            self.roads[rid] = {
                "cls": cls, "name": t.get("name") or "", "v": round(parse_speed(t.get("maxspeed"), v0)),
                "flow": flow, "heavy": heavy, "bus": bus, "oneway": oneway, "len": round(pl.length, 1),
            }
            self.road_lines[rid] = pl
            for i, ((ax, ay), (bx, by)) in enumerate(zip(pts, pts[1:])):
                self.rgrid.add_segment(ax, ay, bx, by, (rid, i))
        print(f"  {len(self.roads)} road segments")

        tunnels = {e["id"] for e in load("rail_ways") if e["tags"].get("tunnel") == "yes"}
        self.lines: Dict[str, Dict[str, Any]] = {}
        self.line_pl: Dict[str, Polyline] = {}
        self.lgrid = Grid(100.0)
        for e in load("routes"):
            t = e.get("tags", {})
            mode = t.get("route")
            if mode not in RAIL:
                continue
            pts: List[Tuple[float, float]] = []
            silent: List[bool] = []  # per segment: tunnel, or a gap in the relation
            n_members = 0
            for m in e.get("members", []):
                if m.get("type") != "way" or m.get("role") not in ("", "forward", "backward") or not m.get("geometry"):
                    continue
                g = [xy(p["lat"], p["lon"]) for p in m["geometry"] if p]
                if len(g) < 2:
                    continue
                tun = m["ref"] in tunnels
                if not pts:
                    pts, silent = g[:], [tun] * (len(g) - 1)
                    n_members = 1
                    continue
                if n_members == 1 and min(math.dist(pts[0], g[0]), math.dist(pts[0], g[-1])) < \
                        min(math.dist(pts[-1], g[0]), math.dist(pts[-1], g[-1])):
                    pts.reverse()
                    silent.reverse()
                n_members += 1
                if math.dist(pts[-1], g[-1]) < math.dist(pts[-1], g[0]):
                    g.reverse()
                if math.dist(pts[-1], g[0]) > 1.0:
                    pts.append(g[0])
                    silent.append(True)
                for q in g[1:]:
                    pts.append(q)
                    silent.append(tun)
            if len(pts) < 2:
                continue
            lid = f"r{e['id']}"
            hsl = t.get("network", "").upper() == "HSL" or t.get("operator", "").startswith("HSL") or len(t.get("ref", "")) <= 2
            if mode == "train":
                headway = 10.0 if hsl else 60.0
            elif mode == "subway":
                headway = 5.0
            else:
                headway = 7.5
            pl = Polyline(pts, silent)
            self.lines[lid] = {"mode": mode, "ref": t.get("ref", ""), "name": t.get("name", ""),
                               "headway": headway, "len": round(pl.length, 1)}
            self.line_pl[lid] = pl
            for i, ((ax, ay), (bx, by)) in enumerate(zip(pts, pts[1:])):
                if not silent[i]:
                    self.lgrid.add_segment(ax, ay, bx, by, (lid, i))
        print(f"  {len(self.lines)} rail routes")

        self.points: Dict[str, Dict[str, Any]] = {}
        self.pgrid = Grid(200.0)
        for e in load("pois"):
            t = e.get("tags", {})
            kind = t.get("amenity") or ("construction" if t.get("landuse") == "construction" else None)
            if kind not in POINT_RANGE or kind == "ferry_terminal":
                continue
            c = e.get("center") or e
            if "lat" not in c:
                continue
            pid = f"{e['type'][0]}{e['id']}"
            self._add_point(pid, kind, c["lat"], c["lon"], t.get("name", ""), 1.0)
        for pid, lat, lon, w, name in VENUES:
            self._add_point(pid, "venue", lat, lon, name, w)
        for pid, lat, lon, kind in FERRIES:
            self._add_point(pid, kind, lat, lon, pid, 1.0)
        print(f"  {len(self.points)} points (bars, schools, sites, venues, ferries)")

        self.runway_ends: Dict[str, Dict[str, Any]] = {}
        for e in load("runways"):
            g = e.get("geometry") or []
            if len(g) < 2 or e.get("tags", {}).get("area") == "yes":
                continue
            a, b = xy(g[0]["lat"], g[0]["lon"]), xy(g[-1]["lat"], g[-1]["lon"])
            L = math.dist(a, b)
            if L < 1500:  # only the big airport's runways
                continue
            ref = e.get("tags", {}).get("ref", str(e["id"]))
            for end, other, name in ((a, b, ref.split("/")[0]), (b, a, ref.split("/")[-1])):
                ux, uy = (end[0] - other[0]) / L, (end[1] - other[1]) / L
                self.runway_ends[f"rw{e['id']}-{slug(name)}"] = {"x": end[0], "y": end[1], "ux": ux, "uy": uy,
                                                                 "len": L, "ref": name}
        print(f"  {len(self.runway_ends)} runway ends")

        self.cgrid = Grid(200.0)
        for e in load("coastline"):
            g = [xy(p["lat"], p["lon"]) for p in e.get("geometry", []) if p]
            for (ax, ay), (bx, by) in zip(g, g[1:]):
                self.cgrid.add_segment(ax, ay, bx, by, (ax, ay, bx, by))
        self.ggrid = Grid(200.0)
        for e in load("green"):
            c = e.get("center")
            if c:
                self.ggrid.add_point(*xy(c["lat"], c["lon"]), 1)

        # Far-field traffic emission per 100 m cell (energy * m^2, at peak).
        self.far_cells: Dict[Tuple[int, int], float] = defaultdict(float)
        for rid, r in self.roads.items():
            if r["cls"] not in FAR_CLASSES:
                continue
            k = self._road_energy_rate(r) * (1 if r["oneway"] else 2)
            pl = self.road_lines[rid]
            for (ax, ay), (bx, by) in zip(pl.pts, pl.pts[1:]):
                L = math.hypot(bx - ax, by - ay)
                n = max(1, int(L // 50))
                for i in range(n):
                    t = (i + 0.5) / n
                    self.far_cells[(int((ax + (bx - ax) * t) // 100), int((ay + (by - ay) * t) // 100))] += k * L / n
        print(f"  indexed in {time.monotonic() - t0:.0f}s")

    def _add_point(self, pid: str, kind: str, lat: float, lon: float, name: str, w: float) -> None:
        x, y = xy(lat, lon)
        self.points[pid] = {"kind": kind, "lat": round(lat, 6), "lon": round(lon, 6), "name": name, "w": w}
        self.pgrid.add_point(x, y, pid)

    @staticmethod
    def _road_energy_rate(r: Dict[str, Any]) -> float:
        """Energy per metre of road per direction: 10^(Lref/10) * 7.5^2 * vehicles/s / speed."""
        v = r["v"] / 3.6
        return 10 ** (mix_lref(r["v"], r["heavy"]) / 10) * 56.25 * (r["flow"] / 3600) / v

    # Screening by buildings between the box and a point, from the 20 m raster.
    def screening(self, x0: float, y0: float, x1: float, y1: float, floor: int, step: float = 20.0) -> float:
        d = math.hypot(x1 - x0, y1 - y0)
        if d < 25:
            return 0.0
        cells = set()
        t = 15.0
        while t < d - 8:
            f = t / d
            cells.add((int((x0 + (x1 - x0) * f) // 20), int((y0 + (y1 - y0) * f) // 20)))
            t += step
        n = sum(1 for c in cells if self.bgrid.cells.get(c))
        if n == 0:
            return 0.0
        return max(3.0, min(25.0, 8 + 3.5 * (n - 1)) - min(6.0, 0.8 * (floor - 1)))

    @staticmethod
    def excess(d: float) -> float:
        """Ground and air absorption beyond plain spreading, dB."""
        return max(0.0, 6.0 * math.log10(d / 50.0)) if d > 50 else 0.0

    # --- Placement ---
    def place(self, n_total: int, hotspot_share: float, seed: int) -> List[Dict[str, Any]]:
        rng = random.Random(seed)
        homes, weights = [], []
        for i, (_, x, y, btype, levels) in enumerate(self.buildings):
            if btype in HOMES:
                default_lv, w = HOMES[btype]
                homes.append(i)
                weights.append(w * min(16.0, levels or default_lv))
        print(f"  {len(homes)} homes to choose from")
        hot = []
        for i, (_, x, y, btype, levels) in enumerate(self.buildings):
            if (btype in HOMES or btype in HOTSPOT_BUILDINGS) and self._is_hotspot(x, y):
                hot.append(i)
        print(f"  {len(hot)} hotspot buildings")

        n_hot = int(n_total * hotspot_share)
        chosen: List[Tuple[int, str]] = []
        taken = Grid(60.0)

        def free(i: int, gap: float) -> bool:
            _, x, y, _, _ = self.buildings[i]
            return all(math.hypot(x - ox, y - oy) >= gap for ox, oy in taken.near(x, y, gap))

        def take(i: int, kind: str) -> None:
            _, x, y, _, _ = self.buildings[i]
            taken.add_point(x, y, (x, y))
            chosen.append((i, kind))

        tries = 0
        while len(chosen) < n_hot and hot and tries < n_hot * 50:
            tries += 1
            i = rng.choice(hot)
            if free(i, 60.0):
                take(i, "hotspot")
        cum = list(_accumulate(weights))
        tries = 0
        while len(chosen) < n_total and tries < n_total * 50:
            tries += 1
            i = homes[_bisect(cum, rng.random() * cum[-1])]
            if free(i, 40.0):
                take(i, "home")
        return [self.describe(i, kind, k + 1, rng) for k, (i, kind) in enumerate(chosen)]

    def _is_hotspot(self, x: float, y: float) -> bool:
        for rid, i in self.rgrid.near(x, y, 30):
            if self.roads[rid]["cls"] in ("motorway", "trunk", "primary"):
                (ax, ay), (bx, by) = self.road_lines[rid].pts[i], self.road_lines[rid].pts[i + 1]
                if seg_closest(x, y, ax, ay, bx, by)[0] < 30:
                    return True
        for pid in self.pgrid.near(x, y, 50):
            p = self.points[pid]
            if p["kind"] in ("bar", "pub", "nightclub"):
                px, py = xy(p["lat"], p["lon"])
                if math.hypot(px - x, py - y) < 50:
                    return True
        return False

    def describe(self, bi: int, kind: str, k: int, rng: random.Random) -> Dict[str, Any]:
        _, bx, by, btype, levels = self.buildings[bi]
        lv = int(levels or HOMES.get(btype, (3, 0))[0])
        floor = rng.randint(1, max(1, lv))

        # Face the nearest busy road (street side) or away from it (courtyard side).
        near_road = None
        for rid, i in self.rgrid.near(bx, by, 120):
            r = self.roads[rid]
            if r["cls"] in ("living_street",):
                continue
            (ax, ay), (cx, cy) = self.road_lines[rid].pts[i], self.road_lines[rid].pts[i + 1]
            d, _, px, py = seg_closest(bx, by, ax, ay, cx, cy)
            score = d / (1 + r["flow"] / 200)
            if near_road is None or score < near_road[0]:
                near_road = (score, px, py, r["name"])
        if near_road and math.hypot(near_road[1] - bx, near_road[2] - by) > 1:
            fx, fy = near_road[1] - bx, near_road[2] - by
            n = math.hypot(fx, fy)
            fx, fy = fx / n, fy / n
        else:
            a = rng.uniform(0, 2 * math.pi)
            fx, fy = math.cos(a), math.sin(a)
        facing = "street" if (kind == "hotspot" or rng.random() < 0.55) else "yard"
        if facing == "yard":
            fx, fy = -fx, -fy
        x, y = bx + 7 * fx, by + 7 * fy  # on the facade
        lat, lon = latlon(x, y)

        def own(px: float, py: float) -> float:
            d = math.hypot(px - x, py - y) or 1.0
            dot = ((px - x) * fx + (py - y) * fy) / d
            return 12.0 if dot < -0.2 else 4.0 if dot < 0.3 else 0.0

        # Roads heard vehicle by vehicle.
        roads = []
        explicit = set()
        by_road: Dict[str, List[int]] = defaultdict(list)
        for rid, i in self.rgrid.near(x, y, 300):
            by_road[rid].append(i)
        for rid, segs in by_road.items():
            r = self.roads[rid]
            pl = self.road_lines[rid]
            d, s, cx, cy, _ = pl.closest(x, y, segs)
            radius = ROAD[r["cls"]][4]
            if d > radius:
                continue
            view = pl.view(x, y, radius + 200)
            frac = min(1.2, view * max(d, 3.0) / math.pi)
            if frac < 0.02:
                continue
            scr = self.screening(x, y, cx, cy, floor) + own(cx, cy)
            roads.append([rid, round(max(d, 3.0), 1), round(s, 1), round(frac, 3), round(scr + self.excess(d), 1)])
            explicit.add(rid)

        # Everything else becomes background: nearby roads not heard individually, then far cells.
        e_bg = 0.0
        for rid, segs in by_road.items():
            if rid in explicit:
                continue
            r = self.roads[rid]
            pl = self.road_lines[rid]
            d, _, cx, cy, _ = pl.closest(x, y, segs)
            view = pl.view(x, y, 500)
            att = self.screening(x, y, cx, cy, floor) + own(cx, cy) + self.excess(max(d, 3))
            e_bg += self._road_energy_rate(r) * (1 if r["oneway"] else 2) * view * 10 ** (-att / 10)
        cx0, cy0 = int(x // 100), int(y // 100)
        for i in range(cx0 - 15, cx0 + 16):
            for j in range(cy0 - 15, cy0 + 16):
                k_e = self.far_cells.get((i, j))
                if not k_e:
                    continue
                px, py = (i + 0.5) * 100, (j + 0.5) * 100
                R = math.hypot(px - x, py - y)
                if R < 300 or R > 1500:
                    continue
                att = self.screening(x, y, px, py, floor, step=50.0) + own(px, py) + self.excess(R)
                e_bg += k_e / (R * R) * 10 ** (-att / 10)
        far_db = 10 * math.log10(e_bg) if e_bg > 0 else 0.0

        # Rail lines.
        lines = []
        by_line: Dict[str, List[int]] = defaultdict(list)
        for lid, i in self.lgrid.near(x, y, 500):
            by_line[lid].append(i)
        for lid, segs in by_line.items():
            pl = self.line_pl[lid]
            d, s, cx, cy, si = pl.closest(x, y, segs)
            if d > (500 if self.lines[lid]["mode"] == "train" else 300) or pl.silent[si]:
                continue
            frac = min(1.2, pl.view(x, y, 700) * max(d, 3.0) / math.pi)
            if frac < 0.05:
                continue
            scr = self.screening(x, y, cx, cy, floor) + own(cx, cy)
            lines.append([lid, round(max(d, 3.0), 1), round(s, 1), round(frac, 3), round(scr + self.excess(d), 1)])

        # Bars, schools, sites, venues, ferries.
        points = []
        for pid in self.pgrid.near(x, y, 1500):
            p = self.points[pid]
            px, py = xy(p["lat"], p["lon"])
            d = math.hypot(px - x, py - y)
            if d > POINT_RANGE[p["kind"]]:
                continue
            scr = self.screening(x, y, px, py, floor) + own(px, py)
            points.append([pid, round(max(d, 5.0), 1), round(scr + self.excess(d), 1)])

        # Flight paths: distance along the extended centreline and to the side of it.
        air = []
        for eid, rw in self.runway_ends.items():
            ax, ay = x - rw["x"], y - rw["y"]
            along = ax * rw["ux"] + ay * rw["uy"]
            side = abs(ax * rw["uy"] - ay * rw["ux"])
            if 0 < along < 16000 and side < 2000 + 0.12 * along:
                air.append([eid, round(along), round(side)])
            elif -rw["len"] < along <= 0 and side < 3000:
                air.append([eid, 0, round(side)])
        air.sort(key=lambda a: math.hypot(a[1] * 0.06 + 60, a[2]))

        # Wind exposure: open shore, greenery, height.
        shore = min((seg_closest(x, y, *s)[0] for s in self.cgrid.near(x, y, 600)), default=2000.0)
        green = min(1.0, sum(1 for _ in self.ggrid.near(x, y, 150)) / 3)
        wind = min(1.0, 0.25 + 0.6 * math.exp(-shore / 250) + 0.2 * green + 0.03 * floor - (0.15 if facing == "yard" else 0))
        density = sum(len(self.bgrid.cells.get((i, j), ())) for i in range(int(x // 20) - 6, int(x // 20) + 7)
                      for j in range(int(y // 20) - 6, int(y // 20) + 7))

        name = near_road[3] if near_road and near_road[3] else btype
        return {
            "id": f"city-{k:04d}-{slug(name)}",
            "lat": round(lat, 6), "lon": round(lon, 6),
            "kind": kind, "building": btype, "floor": floor, "facing": facing,
            "far_db": round(far_db, 1), "density": density, "wind": round(max(0.1, wind), 2),
            "green": round(green, 2),
            "roads": roads, "lines": lines, "points": points, "air": air[:3],
        }


def _accumulate(ws: Sequence[float]) -> Iterable[float]:
    s = 0.0
    for w in ws:
        s += w
        yield s


def _bisect(cum: Sequence[float], v: float) -> int:
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the mock city from OSM data")
    ap.add_argument("--count", type=int, default=3000)
    ap.add_argument("--hotspots", type=float, default=0.1, help="share of boxes placed on hotspots")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default=str(HERE))
    args = ap.parse_args()

    print("[build] indexing OSM data")
    city = City()
    t0 = time.monotonic()
    devices = city.place(args.count, args.hotspots, args.seed)
    print(f"[build] {len(devices)} boxes described in {time.monotonic() - t0:.0f}s")

    used_roads = {r[0] for d in devices for r in d["roads"]}
    used_lines = {r[0] for d in devices for r in d["lines"]}
    used_points = {p[0] for d in devices for p in d["points"]}
    used_air = {a[0] for d in devices for a in d["air"]}
    emitters = {
        "_comment": "Sound sources shared by the city boxes. Built by build_city.py from OpenStreetMap (ODbL).",
        "roads": {k: city.roads[k] for k in sorted(used_roads)},
        "lines": {k: city.lines[k] for k in sorted(used_lines)},
        "points": {k: city.points[k] for k in sorted(used_points)},
        "runway_ends": {k: {"ref": v["ref"]} for k, v in sorted(city.runway_ends.items())},
    }
    out = Path(args.out)
    (out / "emitters.json").write_text(json.dumps(emitters, separators=(",", ":"), ensure_ascii=False))
    with open(out / "devices.json", "w") as f:
        f.write('{"_comment": "Mock boxes on real buildings. Built by build_city.py from OpenStreetMap (ODbL).",\n"devices": [\n')
        f.write(",\n".join(json.dumps(d, separators=(",", ":"), ensure_ascii=False) for d in devices))
        f.write("\n]}\n")
    kinds: Dict[str, int] = defaultdict(int)
    for d in devices:
        kinds[d["kind"]] += 1
    print(f"[build] wrote {out / 'devices.json'} ({dict(kinds)}) and emitters.json "
          f"({len(used_roads)} roads, {len(used_lines)} lines, {len(used_points)} points)")


if __name__ == "__main__":
    main()
