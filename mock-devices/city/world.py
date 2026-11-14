"""The shared city that the boxes built by build_city.py listen to.

Every sound source (a road segment, a tram line, a bar, the airport...) makes
its own events, and every box near it hears the same ones, louder or quieter
depending on distance and screening. A car entering a street passes the boxes
along it one after another; a tram runs its line on a timetable; when a bar
closes, its neighbours all hear the crowd at the same time.

Events depend only on (seed, source, time slot), never on which box asks or
in what order, so a fleet split across processes (--shard) still lives in one
consistent city, and history generated for the past matches what live boxes
would have heard.
"""

from __future__ import annotations

import math
import random
import sys
import zlib
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acoustics import LOCAL_TZ, Event, Profile, SiteSound, Source, Weather, _poisson, curve_value  # noqa: E402

S = Source
ROAD_SLOT = 60.0
LINE_SLOT = 600.0
POINT_SLOT = 60.0
FLIGHT_SLOT = 300.0

RAIL = {  # mode -> (pass speed m/s, travel speed incl. stops m/s, vehicle length m, Lref at 7.5 m, kind)
    "tram": (7.0, 4.5, 30, 79, "tram"),
    "light_rail": (9.0, 6.5, 45, 78, "tram"),
    "subway": (15.0, 11.0, 90, 83, "train"),
    "train": (20.0, 16.0, 110, 86, "train"),
}

# Ferry calls in local time (arrival hour, hours alongside). Approximate winter timetables.
FERRY_CALLS = {
    "ferry-west-harbour": [(6.5, 1.25), (9.5, 1.25), (12.5, 1.25), (15.5, 1.25), (18.5, 1.25), (21.0, 1.5)],
    "ferry-katajanokka": [(10.0, 7.0)],
    "ferry-olympia": [(9.5, 7.5)],
    "ferry-vuosaari-harbour": [(1.0, 4.0), (7.0, 3.0), (11.5, 3.0), (16.0, 3.0), (20.0, 4.0)],
}


def car_lref(v_kmh: float) -> float:
    return 70.0 + 30.0 * math.log10(max(v_kmh, 30.0) / 50.0)


def _rng(*key: Any) -> random.Random:
    return random.Random("|".join(str(k) for k in key))


def _h(*key: Any) -> float:
    """Stable hash in [0, 1)."""
    return zlib.crc32("|".join(str(k) for k in key).encode()) / 2**32


class World:
    """Shared, deterministic event streams. One per process; not thread-safe."""

    def __init__(self, emitters: Dict[str, Any], weather: Weather, seed: int) -> None:
        self.roads = emitters["roads"]
        self.lines = emitters["lines"]
        self.points = emitters["points"]
        self.runway_ends = sorted(emitters.get("runway_ends", {}))
        self.weather = weather
        self.seed = seed
        self._cache: "OrderedDict[Tuple, Any]" = OrderedDict()

    def _cached(self, key: Tuple, make):
        v = self._cache.get(key)
        if v is None:
            v = self._cache[key] = make()
            # Oldest first: the fleet and its backfill move forward in time, so old slots aren't needed again.
            if len(self._cache) > 150_000:
                for _ in range(50_000):
                    self._cache.popitem(last=False)
        return v

    @staticmethod
    def local(t: float) -> datetime:
        return datetime.fromtimestamp(t, LOCAL_TZ)

    # --- Roads ---
    def road_factor(self, name: str, t: float) -> float:
        """Slow ups and downs of one named road, shared by every box along it."""
        p1, p2 = _h("road", name) * 6.283, _h("road2", name) * 6.283
        return math.exp(0.12 * math.sin(2 * math.pi * t / 5400 + p1) + 0.08 * math.sin(2 * math.pi * t / 1300 + p2))

    def vehicles(self, rid: str, direction: int, slot: int) -> List[Tuple[float, str, float, float]]:
        """(entry time, kind, speed m/s, level offset) for vehicles entering the road in this slot."""
        def make():
            r = self.roads[rid]
            t0 = slot * ROAD_SLOT
            local = self.local(t0 + ROAD_SLOT / 2)
            flow = r["flow"] * curve_value("traffic", local) * self.road_factor(r["name"] or rid, t0)
            rng = _rng(self.seed, rid, direction, slot)
            out = []
            bus_share = r["bus"] * curve_value("transit", local) / max(1.0, flow)
            studs = 2.0 if local.month in (11, 12, 1, 2, 3) else 0.0  # studded winter tyres
            for _ in range(_poisson(rng, flow * ROAD_SLOT / 3600)):
                u = rng.random()
                if u < r["heavy"]:
                    kind, dl = "heavy", 9.0
                elif u < r["heavy"] + bus_share:
                    kind, dl = "bus", 7.0
                elif u < r["heavy"] + bus_share + 0.004:
                    kind, dl = "motorbike", 10.0
                else:
                    kind, dl = "car", studs
                v = r["v"] / 3.6 * rng.uniform(0.8, 1.1)
                out.append((t0 + rng.random() * ROAD_SLOT, kind, v, dl + rng.gauss(0, 2.5)))
            return out
        return self._cached(("v", rid, direction, slot), make)

    # --- Rail ---
    def trips(self, lid: str, slot: int) -> List[Tuple[float, float]]:
        """(departure from the start of the line, delay) for trips starting in this slot."""
        def make():
            ln = self.lines[lid]
            t0 = slot * LINE_SLOT
            act = curve_value("transit", self.local(t0))
            if act < 0.05:
                return []
            h = ln["headway"] * 60 / min(1.0, max(act, 0.25))
            phase = _h("line", lid) * h
            rng = _rng(self.seed, lid, slot)
            out = []
            m = math.ceil((t0 - phase) / h)
            while phase + m * h < t0 + LINE_SLOT:
                out.append((phase + m * h, rng.gauss(0, 25) + max(0.0, rng.gauss(0, 60))))
                m += 1
            return out
        return self._cached(("l", lid, slot), make)

    # --- Points: bars, schools, sites, venues, ferries ---
    def point_events(self, pid: str, slot: int) -> List[Tuple[float, str, str, float, float, float]]:
        """(start, kind, shape, size, level at ref_m, ref_m) for events starting in this slot."""
        return self._cached(("p", pid, slot), lambda: self._make_point(pid, slot))

    def _make_point(self, pid: str, slot: int) -> List[Tuple[float, str, str, float, float, float]]:
        p = self.points[pid]
        kind = p["kind"]
        t0 = slot * POINT_SLOT
        local = self.local(t0)
        rng = _rng(self.seed, pid, slot)
        out: List[Tuple[float, str, str, float, float, float]] = []

        def emit(rate_h: float, k: str, shape: str, size: float, size_sd: float, lvl: float, lvl_sd: float, ref: float):
            for _ in range(_poisson(rng, rate_h * POINT_SLOT / 3600)):
                out.append((t0 + rng.random() * POINT_SLOT, k, shape, size * math.exp(rng.gauss(0, size_sd)),
                            lvl + rng.gauss(0, lvl_sd), ref))

        if kind in ("bar", "pub", "nightclub"):
            w = {"nightclub": 2.5, "bar": 1.5, "pub": 1.0}[kind] * (0.6 + 0.8 * _h("size", pid))
            night = curve_value("nightlife", local)
            emit(w * (28 * night + 3 * curve_value("people", local)), "crowd", "speech", 15, 0.7, 66, 4, 5)
            emit(w * 110 * curve_value("closing", local) * (1.6 if kind == "nightclub" else 1.0),
                 "crowd", "speech", 25, 0.6, 70, 4, 5)
            if kind == "nightclub" and night > 0.4 and rng.random() < night:
                out.append((t0, "music", "steady", POINT_SLOT, 72 + rng.gauss(0, 1.5), 5))
        elif kind == "school":
            emit(45 * curve_value("school", local), "children", "speech", 60, 0.6, 72, 4, 15)
        elif kind == "kindergarten":
            emit(35 * curve_value("daycare", local), "children", "speech", 45, 0.6, 67, 4, 15)
        elif kind == "construction":
            if _h("active", pid) < 0.65:  # some OSM sites are finished or idle
                act = curve_value("worksite", local) * (0.5 + _h("busy", pid))
                emit(25 * act, "hammer", "impulse", 30, 0.7, 88, 4, 15)
                emit(20 * act, "machinery", "steady", 120, 0.6, 80, 3, 15)
                emit(12 * act, "drill", "steady", 20, 0.6, 85, 3, 15)
                emit(8 * act, "heavy", "steady", 30, 0.5, 80, 3, 15)
        elif kind == "venue":
            day = local.date()
            dow = local.weekday()
            p_event = {4: 0.55, 5: 0.55, 6: 0.3}.get(dow, 0.2)
            if _h("event", pid, day) < p_event:
                hf = local.hour + local.minute / 60
                w = p["w"]
                if 18.0 <= hf < 19.0:
                    emit(150 * w, "crowd", "speech", 20, 0.6, 70, 3, 30)
                elif 21.75 <= hf < 22.5:  # everyone leaves at once
                    emit(450 * w, "crowd", "speech", 30, 0.6, 72, 3, 30)
                    emit(300 * w, "car", "steady", 20, 0.5, 68, 3, 30)
        elif kind in ("ferry_major", "ferry_cargo"):
            hf = local.hour + local.minute / 60 + local.second / 3600
            for arr, stay in FERRY_CALLS.get(pid, []):
                dep = arr + stay
                for edge in (arr, dep):  # manoeuvring, and a horn when leaving
                    if edge - 0.15 <= hf < edge + 0.1:
                        out.append((t0, "ferry", "steady", POINT_SLOT, 74 + rng.gauss(0, 1), 100))
                if dep <= hf < dep + POINT_SLOT / 3600:
                    out.append((t0 + 20, "horn", "steady", 4, 94, 100))
                    out.append((t0 + 28, "horn", "steady", 4, 94, 100))
                if arr <= hf < dep:  # alongside: engines humming
                    out.append((t0, "ferry", "steady", POINT_SLOT, 63 + rng.gauss(0, 1), 100))
                if arr + 0.1 <= hf < arr + 0.6:  # cars and lorries driving off
                    emit(500, "car", "pass", 1.5, 0.3, 74, 3, 30)
                    emit(150, "heavy", "pass", 1.8, 0.3, 82, 3, 30)
            if kind == "ferry_cargo":
                emit(50 * curve_value("worksite", local) + 10, "heavy", "pass", 2.0, 0.3, 80, 3, 40)
        elif kind == "ferry_small":
            hf = local.hour + local.minute / 60
            if 6.3 <= hf <= 23.5:  # Suomenlinna ferry, every 20 minutes in daytime, 40 in the evening
                period = 20 if hf < 20 else 40
                m = local.hour * 60 + local.minute
                if m % period in (0, 1):
                    out.append((t0, "ferry", "steady", POINT_SLOT, 72 + rng.gauss(0, 1), 50))
        return out

    # --- Flights ---
    def flights(self, slot: int) -> List[Tuple[float, str, str, float]]:
        """(time over the runway end, runway end, arr|dep, level offset)."""
        def make():
            t0 = slot * FLIGHT_SLOT
            local = self.local(t0)
            if not self.runway_ends:
                return []
            block = int(t0 // 10800)  # runway configuration changes every 3 h
            ends = sorted(self.runway_ends, key=lambda e: _h("cfg", block, e))[:2]
            rng = _rng(self.seed, "air", slot)
            out = []
            for _ in range(_poisson(rng, 38 * curve_value("flights", local) * FLIGHT_SLOT / 3600)):
                out.append((t0 + rng.random() * FLIGHT_SLOT, rng.choice(ends), rng.choice(("arr", "dep")),
                            rng.gauss(0, 2.5)))
            return out
        return self._cached(("air", slot), make)


def _excess(d: float) -> float:
    return max(0.0, 6.0 * math.log10(d / 50.0)) if d > 50 else 0.0


def local_profile(spec: Dict[str, Any]) -> Profile:
    """The box's own surroundings: neighbours, the yard, the dog next door."""
    house = spec["building"] in ("house", "detached", "semidetached_house", "terraced", "terrace")
    yard = spec["facing"] == "yard"
    if house:
        srcs = (
            S("voices", "speech", 8, "people", 57, 4, 6, 0.6),
            S("dog", "impulse", 5, "dogs", 67, 4, 6, 0.6, 1.2),
            S("car", "pass", 6, "traffic", 64, 3, 1.2),  # the neighbours' cars
            S("machinery", "steady", 1.0, "people", 68, 4, 240, 0.6),  # leaf blowers, snow blowers
        )
    else:
        srcs = (
            S("voices", "speech", 22 if yard else 30, "people", 58, 4, 5, 0.6),
            S("children", "speech", 5 if yard else 2, "people", 62, 4, 25, 0.6),
            S("dog", "impulse", 2, "dogs", 66, 4, 5, 0.6, 1.2),
            S("car", "pass", 4 if yard else 10, "traffic", 61, 3, 1.2),  # parking, deliveries
            S("heavy", "steady", 0.04, "worksite", 74, 3, 120, 0.4),  # refuse lorry, ~twice a week
        )
    density = spec.get("density", 0)
    hvac = 33 + min(12.0, density / 15) if density > 30 else None
    return Profile(f"city:{spec['kind']}", 0, 0, srcs, wind_exposure=spec.get("wind", 0.5), hvac_db=hvac)


class CitySound(SiteSound):
    """SiteSound whose background and events come from the shared World."""

    def __init__(self, spec: Dict[str, Any], world: World, **kw: Any) -> None:
        self.spec = spec
        self.world = world
        self.floor_db = 24.0 + min(6.0, spec.get("density", 0) / 30) - 2.0 * spec.get("green", 0)
        super().__init__(local_profile(spec), world.weather, **kw)

    def background_db(self, t: float) -> float:
        local = datetime.fromtimestamp(t, LOCAL_TZ)
        far = 10 ** (self.spec["far_db"] / 10) * curve_value("traffic", local) if self.spec["far_db"] > 0 else 0.0
        return 10 * math.log10(far + 10 ** (self.floor_db / 10)) + self.level_bias_db + self._drift

    def _spawn_extra(self, start: float, until: float) -> None:
        w = self.world
        cut = self.background_db(start) - 12.0  # inaudible under the background
        add = self.events.append

        for rid, d, s, frac, att in self.spec["roads"]:
            r = w.roads[rid]
            gain = 10 * math.log10(frac) - att - 20 * math.log10(d / 7.5)
            if car_lref(r["v"]) + 9 + gain < cut:
                continue
            v_nom = r["v"] / 3.6
            for direction in ((0,) if r["oneway"] else (0, 1)):
                pos = s if direction == 0 else r["len"] - s
                tau_max = d / (0.8 * v_nom)
                lo = start - pos / (0.8 * v_nom)
                hi = until - pos / (1.1 * v_nom) + 6 * tau_max
                for slot in range(int(lo // ROAD_SLOT), int(hi // ROAD_SLOT) + 1):
                    for t_in, kind, v, dl in w.vehicles(rid, direction, slot):
                        tau = max(0.3, d / v)
                        t_pass = t_in + pos / v
                        if start <= t_pass - 6 * tau < until:
                            lp = car_lref(r["v"] * v / v_nom) + dl + gain
                            if lp >= cut:
                                add(Event(kind, "pass", t_pass, tau, lp, 0.0))

        for lid, d, s, frac, att in self.spec["lines"]:
            ln = w.lines[lid]
            v_pass, v_trav, length, lref, kind = RAIL[ln["mode"]]
            tau = math.hypot(d, length / 2) / v_pass
            lp0 = lref + 10 * math.log10(frac) - att - 20 * math.log10(d / 7.5)
            if lp0 + 3 < cut:
                continue
            lo = start - s / v_trav - 200
            hi = until - s / v_trav + 6 * tau + 200
            for slot in range(int(lo // LINE_SLOT), int(hi // LINE_SLOT) + 1):
                for t_dep, delay in w.trips(lid, slot):
                    t_pass = t_dep + delay + s / v_trav
                    if start <= t_pass - 6 * tau < until:
                        jitter = (_h(lid, t_dep) - 0.5) * 4
                        add(Event(kind, "pass", t_pass, tau, lp0 + jitter, 0.0))

        for pid, d, att in self.spec["points"]:
            for slot in range(int((start - POINT_SLOT) // POINT_SLOT), int(until // POINT_SLOT) + 1):
                for t0, kind, shape, size, lvl, ref in w.point_events(pid, slot):
                    if start <= t0 < until:
                        lp = lvl - 20 * math.log10(max(d, ref) / ref) - att
                        if lp >= cut:
                            if shape == "pass":
                                add(Event(kind, "pass", t0 + 6 * size, size, lp, 0.0))
                            else:
                                add(Event(kind, shape, t0, size, lp, 1.6))

        if self.spec["air"]:
            ends = {e: (along, side) for e, along, side in self.spec["air"]}
            for slot in range(int((start - 300) // FLIGHT_SLOT), int(until // FLIGHT_SLOT) + 1):
                for t_f, end, op, dl in w.flights(slot):
                    if end not in ends:
                        continue
                    along, side = ends[end]
                    alt = (15 + along * 0.052) if op == "arr" else (250 + along * 0.09)
                    slant = math.hypot(side, alt)
                    v = 75.0 if op == "arr" else 90.0
                    tau = max(2.0, slant / v)
                    lp = 101 + (5 if op == "dep" else 0) - 20 * math.log10(max(slant, 50) / 100) - 0.5 * _excess(slant) + dl
                    t_pass = t_f + (along / v if op == "dep" else -along / v)
                    if start <= t_pass - 6 * tau < until and lp >= cut:
                        add(Event("aircraft", "pass", t_pass, tau, lp, 0.0))
