"""Sound model for the mock boxes.

Each mock box simulates the sound at its site as a sequence of short blocks
(125 ms, like the real box) and summarises them exactly the way
night-noise-box/python/analysis.py does: LAeq is the energy mean of the
blocks, Lmax the loudest block, L90 the 10th percentile. So the numbers stay
consistent with each other (L90 <= LAeq <= Lmax) for the same reasons they do
on a real box.

The sound at a site is:

  * a background level that follows the site's day/night curve, drifts slowly
    and is raised by city-wide wind and rain;
  * discrete sound events (cars passing, trams, voices, hammering, ferries,
    aircraft...) that arrive at a rate that follows a time-of-day curve in
    Helsinki local time, weekday or weekend.

The classifier is faked from the energy each kind of sound contributes to each
1 s window, passed through a confusion table onto the labels the real Edge
Impulse model has: background, construction, noise, traffic, voices.

Standard library only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Helsinki")
except Exception:  # no tz database: Helsinki winter time is close enough
    LOCAL_TZ = timezone(timedelta(hours=2))

# Labels of the real model (night-noise-linux-aarch64-v2-impulse).
MODEL_LABELS = ("background", "construction", "noise", "traffic", "voices")

# What the classifier calls each physical sound. Rows sum to 1.
CONFUSION: Dict[str, Dict[str, float]] = {
    "ambient":   {"background": 0.88, "noise": 0.07, "traffic": 0.05},
    "car":       {"traffic": 0.84, "noise": 0.09, "background": 0.05, "construction": 0.02},
    "heavy":     {"traffic": 0.76, "construction": 0.12, "noise": 0.12},
    "bus":       {"traffic": 0.80, "noise": 0.12, "construction": 0.08},
    "tram":      {"traffic": 0.66, "noise": 0.22, "construction": 0.12},
    "train":     {"traffic": 0.72, "noise": 0.24, "construction": 0.04},
    "aircraft":  {"traffic": 0.44, "noise": 0.44, "background": 0.12},
    "motorbike": {"traffic": 0.78, "noise": 0.16, "construction": 0.06},
    "siren":     {"noise": 0.62, "traffic": 0.34, "voices": 0.04},
    "voices":    {"voices": 0.86, "noise": 0.08, "background": 0.06},
    "crowd":     {"voices": 0.74, "noise": 0.18, "background": 0.08},
    "children":  {"voices": 0.80, "noise": 0.16, "background": 0.04},
    "music":     {"noise": 0.52, "voices": 0.33, "background": 0.15},
    "dog":       {"noise": 0.58, "voices": 0.32, "background": 0.10},
    "hammer":    {"construction": 0.82, "noise": 0.16, "traffic": 0.02},
    "machinery": {"construction": 0.64, "traffic": 0.24, "noise": 0.12},
    "drill":     {"construction": 0.74, "noise": 0.24, "traffic": 0.02},
    "ferry":     {"noise": 0.48, "traffic": 0.40, "background": 0.12},
    "horn":      {"noise": 0.70, "traffic": 0.30},
    "gulls":     {"noise": 0.50, "voices": 0.30, "background": 0.20},
    "waves":     {"background": 0.62, "noise": 0.38},
    "wind":      {"noise": 0.66, "background": 0.34},
    "rain":      {"background": 0.58, "noise": 0.42},
    "hvac":      {"background": 0.80, "noise": 0.20},
}


# --- Time-of-day curves -------------------------------------------------------
# 24 hourly values (local time), linearly interpolated. A curve is chosen by
# day type: "wd" Monday-Friday, "sat", "sun". Missing "sat"/"sun" fall back to
# "we", then "wd".

def _c(*vals: float) -> List[float]:
    assert len(vals) == 24
    return list(vals)


CURVES: Dict[str, Dict[str, List[float]]] = {
    # General urban activity; also drives the background between night and day.
    "urban": {
        "wd": _c(.15, .08, .05, .05, .10, .30, .70, .95, 1, .90, .85, .85, .85, .85, .88, .95, 1, .95, .80, .65, .55, .45, .35, .25),
        "we": _c(.25, .18, .12, .08, .06, .08, .15, .30, .45, .60, .72, .80, .85, .85, .82, .80, .78, .72, .65, .58, .50, .42, .35, .30),
    },
    # Road traffic with morning and afternoon rush hours.
    "traffic": {
        "wd": _c(.10, .06, .04, .04, .08, .28, .70, 1, .92, .72, .66, .68, .70, .70, .76, .90, 1, .92, .72, .55, .45, .36, .28, .18),
        "we": _c(.20, .15, .10, .07, .05, .08, .15, .25, .40, .55, .66, .72, .74, .74, .72, .70, .68, .64, .56, .48, .40, .34, .28, .24),
    },
    # Trams, buses and trains: no service roughly 02-05.
    "transit": {
        "wd": _c(.25, .10, 0, 0, 0, .30, .80, 1, 1, .75, .65, .65, .65, .65, .70, .90, 1, .95, .75, .60, .55, .50, .45, .35),
        "we": _c(.40, .25, .05, 0, 0, .15, .35, .45, .55, .60, .65, .65, .65, .65, .65, .65, .65, .60, .55, .50, .50, .48, .45, .42),
    },
    # Pedestrians, shoppers, people in squares.
    "people": {
        "wd": _c(.06, .03, .02, .02, .02, .05, .20, .55, .70, .60, .65, .85, 1, .85, .70, .80, .95, .90, .70, .55, .45, .35, .20, .10),
        "we": _c(.15, .10, .05, .03, .02, .02, .05, .12, .25, .45, .65, .85, 1, 1, .95, .90, .85, .75, .65, .55, .45, .35, .25, .18),
    },
    # Bars and clubs. Friday and Saturday nights; Thursday a bit.
    "nightlife": {
        "wd": _c(.10, .05, .02, 0, 0, 0, 0, 0, 0, 0, 0, .02, .05, .05, .05, .05, .08, .15, .25, .30, .35, .40, .40, .25),
        "thu": _c(.12, .06, .02, 0, 0, 0, 0, 0, 0, 0, 0, .02, .05, .05, .05, .05, .08, .15, .25, .35, .45, .55, .60, .55),
        "fri": _c(.30, .15, .05, 0, 0, 0, 0, 0, 0, 0, 0, .02, .05, .05, .05, .08, .12, .25, .40, .55, .70, .85, .95, 1),
        "sat": _c(1, .95, .75, .40, .10, 0, 0, 0, 0, 0, 0, .03, .05, .08, .10, .12, .15, .25, .40, .55, .70, .85, .95, 1),
        "sun": _c(1, .95, .75, .40, .10, 0, 0, 0, 0, 0, 0, .03, .05, .08, .10, .10, .10, .12, .15, .15, .15, .12, .10, .08),
    },
    # Noisy construction is allowed on weekdays 7-18 (lunch dip), Saturdays lightly.
    "worksite": {
        "wd": _c(0, 0, 0, 0, 0, 0, .05, .80, 1, 1, 1, .55, .40, 1, 1, .95, .85, .40, .05, 0, 0, 0, 0, 0),
        "sat": _c(0, 0, 0, 0, 0, 0, 0, 0, 0, .25, .35, .35, .20, .30, .25, .10, 0, 0, 0, 0, 0, 0, 0, 0),
        "sun": [0.0] * 24,
    },
    # School yard: breaks around 9:45, 11:15 (lunch) and 13:15, weekdays only.
    "school": {
        "wd": _c(0, 0, 0, 0, 0, 0, 0, .05, .30, 1, .35, 1, .80, 1, .30, .20, .10, .05, .10, .10, .05, 0, 0, 0),
        "we": _c(0, 0, 0, 0, 0, 0, 0, 0, 0, .05, .10, .15, .15, .15, .15, .10, .05, 0, 0, 0, 0, 0, 0, 0),
    },
    # Helsinki-Vantaa: busy 06-23, few night flights.
    "flights": {
        "wd": _c(.10, .05, .03, .03, .05, .25, .90, 1, .85, .75, .80, .80, .75, .75, .80, .85, .90, 1, .90, .85, .80, .70, .55, .30),
        "we": _c(.10, .05, .03, .03, .05, .20, .75, .85, .80, .75, .75, .75, .75, .75, .75, .80, .85, .90, .85, .80, .75, .65, .50, .25),
    },
    # Ferry departures and arrivals at the harbours.
    "ferries": {
        "wd": _c(0, 0, 0, 0, 0, 0, .30, 1, .60, .50, 1, .60, .50, .80, .50, .60, 1, .80, .60, 1, .50, .70, .30, .10),
        "we": _c(0, 0, 0, 0, 0, 0, .20, .80, .60, .60, 1, .60, .60, .90, .60, .60, 1, .80, .60, 1, .60, .70, .30, .10),
    },
    # Dog walking: morning and evening.
    "dogs": {
        "wd": _c(.05, .02, 0, 0, .02, .15, .70, 1, .60, .30, .20, .20, .25, .25, .25, .30, .50, .80, .90, .80, .60, .45, .30, .15),
        "we": _c(.08, .04, 0, 0, 0, .05, .20, .50, .80, .90, .70, .50, .40, .40, .45, .50, .60, .70, .80, .70, .55, .45, .30, .15),
    },
    # Daycare outdoor play: mid-morning and afternoon, weekdays.
    "daycare": {
        "wd": _c(0, 0, 0, 0, 0, 0, 0, 0, .10, .60, 1, .60, .10, .05, .40, 1, .80, .20, 0, 0, 0, 0, 0, 0),
        "we": [0.0] * 24,
    },
    # Bars closing: people pour out around 02-04 on Friday and Saturday nights.
    "closing": {
        "wd": _c(.10, .15, .10, .02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .05),
        "fri": _c(.20, .30, .20, .05, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .05),
        "sat": _c(.40, .70, 1, .80, .30, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .05),
        "sun": _c(.40, .70, 1, .80, .30, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    },
    # Building ventilation: always on, a little higher in office hours.
    "hvac": {"wd": _c(*([.85] * 7 + [1] * 11 + [.85] * 6)), "we": [0.85] * 24},
    "always": {"wd": [1.0] * 24},
}

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def curve_value(name: str, local: datetime) -> float:
    c = CURVES[name]
    dow = local.weekday()
    key = _DAY_KEYS[dow]
    if key in c:
        vals = c[key]
    elif dow >= 5 and "we" in c:
        vals = c["we"]
    else:
        vals = c["wd"]
    hf = local.hour + local.minute / 60.0 + local.second / 3600.0
    h = int(hf) % 24
    nxt = vals[(h + 1) % 24]
    return vals[h] + (nxt - vals[h]) * (hf - int(hf))


# --- Sources and sites --------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """One kind of sound event at a site.

    shape  pass     point source driving by: lp - 10 log10(1 + (dt/size)^2)
           steady   roughly constant for `size` seconds (idling, machinery, music)
           speech   fluctuating, with pauses, for `size` seconds
           impulse  hits at `hits_per_s` for `size` seconds (hammering)
    rate   events per hour when the curve is at 1
    lp     peak level at the box, dB; lp_sd its spread between events
    size   tau (pass) or duration (others), seconds; lognormal spread `size_sd`
    """

    kind: str
    shape: str
    rate: float
    curve: str
    lp: float
    lp_sd: float = 3.0
    size: float = 1.0
    size_sd: float = 0.3
    hits_per_s: float = 1.5


@dataclass(frozen=True)
class Profile:
    """A type of site. Levels are at the box, in dB."""

    name: str
    bg_night: float  # background (roughly L90) in the quietest hours
    bg_day: float  # background at full urban activity
    sources: Tuple[Source, ...]
    wind_exposure: float = 0.5  # 0 sheltered courtyard ... 1 open shore
    bg_curve: str = "urban"
    hvac_db: Optional[float] = None  # steady ventilation hum, if any


S = Source

PROFILES: Dict[str, Profile] = {p.name: p for p in [
    Profile("motorway", 46, 58, (
        S("car", "pass", 4200, "traffic", 64, 2.5, 1.1),
        S("heavy", "pass", 320, "traffic", 72, 3, 1.4),
        S("motorbike", "pass", 25, "traffic", 76, 3, 0.9),
        S("siren", "pass", 0.6, "traffic", 80, 3, 6),
    ), wind_exposure=0.6),
    Profile("arterial", 44, 57, (
        S("car", "pass", 1600, "traffic", 66, 2.5, 0.8),
        S("bus", "pass", 50, "transit", 75, 2, 1.6),
        S("heavy", "pass", 70, "traffic", 76, 3, 1.2),
        S("motorbike", "pass", 15, "traffic", 82, 3, 0.7),
        S("voices", "speech", 90, "people", 62, 4, 6, 0.6),
        S("siren", "pass", 1.2, "traffic", 86, 3, 6),
    ), wind_exposure=0.4),
    Profile("arterial_tram", 45, 58, (
        S("car", "pass", 1300, "traffic", 65, 2.5, 0.8),
        S("bus", "pass", 45, "transit", 75, 2, 1.6),
        S("tram", "pass", 32, "transit", 74, 2, 3.5),
        S("heavy", "pass", 40, "traffic", 76, 3, 1.2),
        S("voices", "speech", 180, "people", 63, 4, 6, 0.6),
        S("siren", "pass", 1.5, "traffic", 86, 3, 6),
    ), wind_exposure=0.3),
    Profile("transit_hub", 48, 60, (
        S("bus", "pass", 120, "transit", 74, 2, 1.8),
        S("bus", "steady", 40, "transit", 66, 2, 25, 0.5),
        S("tram", "pass", 45, "transit", 73, 2, 3.5),
        S("crowd", "speech", 160, "people", 62, 3, 15, 0.6),
        S("voices", "speech", 300, "people", 62, 4, 5, 0.6),
        S("siren", "pass", 1.0, "traffic", 84, 3, 6),
    ), wind_exposure=0.3),
    Profile("plaza", 42, 53, (
        S("voices", "speech", 260, "people", 63, 4, 6, 0.6),
        S("crowd", "speech", 40, "people", 66, 3, 30, 0.6),
        S("tram", "pass", 25, "transit", 72, 2, 3),
        S("car", "pass", 60, "traffic", 66, 3, 1.2),
        S("gulls", "speech", 6, "always", 70, 4, 4, 0.5),
    ), wind_exposure=0.6),
    Profile("market_harbour", 43, 54, (
        S("voices", "speech", 220, "people", 63, 4, 6, 0.6),
        S("car", "pass", 400, "traffic", 65, 3, 1),
        S("bus", "pass", 20, "transit", 72, 2, 1.6),
        S("ferry", "steady", 3, "ferries", 64, 3, 240, 0.4),
        S("horn", "steady", 1.5, "ferries", 84, 3, 3, 0.3),
        S("gulls", "speech", 20, "always", 72, 4, 5, 0.5),
    ), wind_exposure=0.9),
    Profile("nightlife", 40, 52, (
        S("car", "pass", 450, "traffic", 67, 3, 0.8),
        S("tram", "pass", 20, "transit", 74, 2, 3),
        S("voices", "speech", 140, "people", 63, 4, 6, 0.6),
        S("crowd", "speech", 200, "nightlife", 65, 4, 25, 0.7),
        S("music", "steady", 30, "nightlife", 60, 3, 90, 0.8),
        S("siren", "pass", 1.5, "nightlife", 85, 3, 6),
        S("motorbike", "pass", 10, "traffic", 80, 3, 0.7),
    ), wind_exposure=0.3),
    Profile("construction", 42, 54, (
        S("hammer", "impulse", 30, "worksite", 81, 4, 30, 0.7, 1.6),
        S("machinery", "steady", 25, "worksite", 69, 3, 120, 0.6),
        S("drill", "steady", 18, "worksite", 74, 3, 20, 0.6),
        S("heavy", "pass", 40, "worksite", 81, 3, 1.6),
        S("car", "pass", 250, "traffic", 66, 3, 1),
        S("voices", "speech", 60, "worksite", 62, 4, 5, 0.6),
    ), wind_exposure=0.6),
    Profile("harbour_ferry", 45, 55, (
        S("ferry", "steady", 2.5, "ferries", 70, 3, 600, 0.4),
        S("horn", "steady", 1.2, "ferries", 88, 3, 3, 0.3),
        S("car", "pass", 700, "ferries", 70, 3, 1),
        S("heavy", "pass", 160, "ferries", 80, 3, 1.5),
        S("tram", "pass", 12, "transit", 73, 2, 3),
        S("voices", "speech", 80, "ferries", 62, 4, 6, 0.6),
        S("gulls", "speech", 12, "always", 72, 4, 5, 0.5),
    ), wind_exposure=1.0),
    Profile("residential_street", 33, 46, (
        S("car", "pass", 180, "traffic", 66, 3, 1),
        S("bus", "pass", 6, "transit", 73, 2, 1.6),
        S("voices", "speech", 40, "people", 60, 4, 5, 0.6),
        S("dog", "impulse", 4, "dogs", 70, 4, 6, 0.6, 1.2),
        S("children", "speech", 8, "school", 64, 4, 20, 0.6),
    ), wind_exposure=0.4),
    Profile("courtyard", 31, 41, (
        S("voices", "speech", 25, "people", 58, 4, 5, 0.6),
        S("children", "speech", 6, "people", 62, 4, 25, 0.6),
        S("car", "pass", 20, "traffic", 62, 3, 1.5),
        S("dog", "impulse", 2, "dogs", 66, 4, 5, 0.6, 1.2),
    ), wind_exposure=0.15, hvac_db=38),
    Profile("park", 35, 47, (
        S("car", "pass", 120, "traffic", 59, 3, 2.5),
        S("voices", "speech", 70, "people", 60, 4, 6, 0.6),
        S("dog", "impulse", 10, "dogs", 68, 4, 6, 0.6, 1.2),
        S("children", "speech", 10, "people", 63, 4, 30, 0.6),
    ), wind_exposure=0.8),
    Profile("forest_park", 28, 38, (
        S("voices", "speech", 15, "people", 57, 4, 6, 0.6),
        S("dog", "impulse", 5, "dogs", 64, 4, 6, 0.6, 1.2),
        S("train", "pass", 8, "transit", 58, 3, 10),
        S("aircraft", "pass", 3, "flights", 56, 3, 15),
    ), wind_exposure=0.7),
    Profile("shore", 36, 45, (
        S("waves", "steady", 50, "always", 50, 4, 40, 0.5),
        S("voices", "speech", 25, "people", 59, 4, 6, 0.6),
        S("dog", "impulse", 6, "dogs", 66, 4, 6, 0.6, 1.2),
        S("gulls", "speech", 12, "always", 68, 4, 5, 0.5),
        S("car", "pass", 80, "traffic", 60, 3, 2),
    ), wind_exposure=1.0),
    Profile("island", 30, 39, (
        S("ferry", "steady", 2.5, "ferries", 64, 3, 180, 0.4),
        S("voices", "speech", 30, "people", 59, 4, 6, 0.6),
        S("gulls", "speech", 10, "always", 68, 4, 5, 0.5),
        S("waves", "steady", 30, "always", 47, 4, 40, 0.5),
    ), wind_exposure=0.9),
    Profile("rail", 40, 50, (
        S("train", "pass", 30, "transit", 82, 3, 6),
        S("train", "pass", 1.5, "always", 85, 3, 18),  # night freight
        S("car", "pass", 200, "traffic", 64, 3, 1.2),
        S("voices", "speech", 70, "people", 61, 4, 5, 0.6),
    ), wind_exposure=0.5),
    Profile("station", 44, 55, (
        S("train", "pass", 36, "transit", 79, 3, 6),
        S("horn", "steady", 3, "transit", 80, 3, 1.5, 0.3),
        S("bus", "pass", 50, "transit", 75, 2, 1.6),
        S("voices", "speech", 220, "people", 63, 4, 5, 0.6),
        S("crowd", "speech", 50, "transit", 65, 3, 20, 0.6),
    ), wind_exposure=0.4),
    Profile("airport", 42, 52, (
        S("aircraft", "pass", 24, "flights", 82, 4, 10),
        S("car", "pass", 500, "traffic", 66, 3, 1),
        S("bus", "pass", 30, "transit", 73, 2, 1.6),
    ), wind_exposure=0.8),
    Profile("school", 34, 47, (
        S("children", "speech", 50, "school", 72, 4, 60, 0.6),
        S("voices", "speech", 30, "people", 60, 4, 5, 0.6),
        S("car", "pass", 150, "traffic", 65, 3, 1),
        S("bus", "pass", 6, "transit", 72, 2, 1.6),
    ), wind_exposure=0.5),
    Profile("campus", 37, 50, (
        S("voices", "speech", 160, "people", 62, 4, 6, 0.6),
        S("crowd", "speech", 20, "people", 64, 3, 30, 0.6),
        S("bus", "pass", 30, "transit", 74, 2, 1.6),
        S("car", "pass", 200, "traffic", 64, 3, 1),
        S("drill", "steady", 2, "worksite", 74, 3, 15, 0.6),
    ), wind_exposure=0.5, hvac_db=42),
    Profile("mall_hub", 45, 56, (
        S("bus", "pass", 70, "transit", 75, 2, 1.8),
        S("car", "pass", 600, "traffic", 66, 3, 1),
        S("voices", "speech", 260, "people", 63, 4, 5, 0.6),
        S("crowd", "speech", 50, "people", 65, 3, 20, 0.6),
    ), wind_exposure=0.4, hvac_db=50),
    Profile("suburban", 30, 43, (
        S("car", "pass", 70, "traffic", 64, 3, 1.2),
        S("voices", "speech", 20, "people", 58, 4, 5, 0.6),
        S("dog", "impulse", 4, "dogs", 68, 4, 6, 0.6, 1.2),
        S("aircraft", "pass", 2, "flights", 62, 3, 15),
    ), wind_exposure=0.5),
    Profile("rural", 24, 32, (
        S("car", "pass", 10, "traffic", 62, 3, 1.5),
        S("aircraft", "pass", 3, "flights", 55, 3, 18),
        S("voices", "speech", 6, "people", 56, 4, 6, 0.6),
        S("dog", "impulse", 2, "dogs", 62, 4, 5, 0.6, 1.2),
    ), wind_exposure=0.9),
]}


# --- Weather ------------------------------------------------------------------

class Weather:
    """City-wide wind (m/s) and rain (0..1), shared by every box.

    A smooth deterministic function of time, so history generated for the past
    and live readings agree and every box sees the same weather.
    """

    def __init__(self, seed: int) -> None:
        r = random.Random(seed ^ 0x5EED)
        self._wp = [r.uniform(0, 2 * math.pi) for _ in range(3)]
        self._rp = [r.uniform(0, 2 * math.pi) for _ in range(3)]

    def wind(self, t: float) -> float:
        w = (5.5 + 2.8 * math.sin(2 * math.pi * t / 31_000 + self._wp[0])
             + 1.6 * math.sin(2 * math.pi * t / 8_300 + self._wp[1])
             + 0.8 * math.sin(2 * math.pi * t / 2_100 + self._wp[2]))
        return max(0.0, w)

    def rain(self, t: float) -> float:
        r = (math.sin(2 * math.pi * t / 26_000 + self._rp[0])
             + 0.5 * math.sin(2 * math.pi * t / 6_100 + self._rp[1])
             + 0.25 * math.sin(2 * math.pi * t / 1_300 + self._rp[2]) - 0.75)
        return min(1.0, max(0.0, r))


# --- Events -------------------------------------------------------------------

@dataclass
class Event:
    kind: str
    shape: str
    t0: float  # start (steady/speech/impulse) or closest approach (pass), epoch s
    size: float
    lp: float
    hits_per_s: float
    start: float = field(init=False)
    end: float = field(init=False)

    def __post_init__(self) -> None:
        if self.shape == "pass":
            self.start, self.end = self.t0 - 6 * self.size, self.t0 + 6 * self.size
        else:
            self.start, self.end = self.t0, self.t0 + self.size

    def level(self, t: float, block_s: float, rng: random.Random) -> Optional[float]:
        if t < self.start or t >= self.end:
            return None
        if self.shape == "pass":
            dt = (t - self.t0) / self.size
            return self.lp - 10.0 * math.log10(1.0 + dt * dt)
        edge = min(t - self.t0, self.end - t)
        ramp = -12.0 * (1.0 - edge / 0.8) if edge < 0.8 else 0.0
        if self.shape == "steady":
            return self.lp + ramp + rng.gauss(0, 1.0)
        if self.shape == "speech":
            gap = -14.0 if rng.random() < 0.3 else 0.0
            return self.lp + ramp + gap + rng.gauss(0, 3.0)
        # impulse: ~125 ms hits at hits_per_s over a quieter base, energy-averaged
        # over the block so the result doesn't depend on the block length.
        hits = _poisson(rng, self.hits_per_s * block_s)
        frac = min(1.0, hits * 0.125 / block_s)
        base = _db_to_e(self.lp - 22.0 + rng.gauss(0, 2.0))
        return _e_to_db(frac * _db_to_e(self.lp + rng.gauss(0, 2.0)) + (1.0 - frac) * base)


def _db_to_e(db: float) -> float:
    return 10.0 ** (db / 10.0)


def _e_to_db(e: float) -> float:
    return 10.0 * math.log10(e) if e > 0 else -120.0


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Same interpolation as night-noise-box/python/analysis.py."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def utc_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- One site's sound ---------------------------------------------------------

@dataclass
class SiteSound:
    """The evolving sound at one box. Not thread-safe; one caller at a time."""

    profile: Profile
    weather: Weather
    seed: int
    level_bias_db: float = 0.0  # this site is louder/quieter than its profile
    cal_error_db: float = 0.0  # mic sensitivity error not fixed by calibration
    clip_db: float = 94.0  # 0 dBFS in dB, i.e. the calibration offset
    has_model: bool = True
    dropout_rate: float = 0.002  # chance a reading loses some audio
    rng: random.Random = field(init=False)
    events: List[Event] = field(default_factory=list, init=False)
    _drift: float = field(default=0.0, init=False)
    _spawned_until: Optional[float] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def _spawn(self, until: float) -> None:
        """Create events whose activity starts before `until`."""
        # On the first call, start 15 min back so long events are already under way.
        start = self._spawned_until if self._spawned_until is not None else until - 900.0
        if until <= start:
            return
        rng = self.rng
        local = datetime.fromtimestamp((start + until) / 2, LOCAL_TZ)
        span_h = (until - start) / 3600.0
        for src in self.profile.sources:
            lam = src.rate * curve_value(src.curve, local) * span_h
            n = _poisson(rng, lam)
            for _ in range(n):
                size = src.size * math.exp(rng.gauss(0, src.size_sd))
                lp = src.lp + self.level_bias_db + rng.gauss(0, src.lp_sd)
                t_first = start + rng.random() * (until - start)
                t0 = t_first + 6 * size if src.shape == "pass" else t_first
                self.events.append(Event(src.kind, src.shape, t0, size, lp, src.hits_per_s))
        self._spawn_extra(start, until)
        self._spawned_until = until

    def _spawn_extra(self, start: float, until: float) -> None:
        """Hook for sounds that don't come from the profile (see city/world.py)."""

    def background_db(self, t: float) -> float:
        p = self.profile
        local = datetime.fromtimestamp(t, LOCAL_TZ)
        act = curve_value(p.bg_curve, local)
        return p.bg_night + (p.bg_day - p.bg_night) * act + self.level_bias_db + self._drift

    def reading(self, t_start: float, duration_s: float, block_s: float = 0.125) -> Dict[str, object]:
        """One reading over [t_start, t_start + duration_s), like the box's ReadingAccumulator."""
        rng = self.rng
        t_end = t_start + duration_s
        self._drift = 0.97 * self._drift + rng.gauss(0, 0.35)
        self._spawn(t_end)

        n_blocks = max(1, int(round(duration_s / block_s)))
        lost = 0
        if rng.random() < self.dropout_rate:
            lost = min(n_blocks - 1, int(rng.uniform(0.5, 4.0) / block_s))
        lost_from = rng.randrange(0, n_blocks - lost + 1) if lost else n_blocks

        win_blocks = max(1, int(round(1.0 / block_s)))  # classifier window ~1 s
        p = self.profile
        wind = self.weather.wind(t_start) * p.wind_exposure
        rain = self.weather.rain(t_start)
        bg = self.background_db(t_start)
        active = [e for e in self.events if e.start < t_end and e.end > t_start]

        levels: List[float] = []
        clipped = 0
        score_sums: Dict[str, float] = {}
        inferences = 0
        win_e: Dict[str, float] = {}
        win_n = 0
        for i in range(n_blocks):
            if lost_from <= i < lost_from + lost:
                continue
            t = t_start + (i + 0.5) * block_s
            e: Dict[str, float] = {"ambient": _db_to_e(bg + rng.gauss(0, 0.8))}
            if p.hvac_db is not None:
                e["hvac"] = _db_to_e(p.hvac_db + rng.gauss(0, 0.5))
            if wind > 4.0:  # wind noise on the mic, despite the foam
                gust = wind * (1.0 + 0.25 * rng.gauss(0, 1))
                if gust > 4.0:
                    e["wind"] = _db_to_e(18.0 + 3.0 * gust)
            if rain > 0.0:
                e["rain"] = _db_to_e(34.0 + 14.0 * rain + rng.gauss(0, 0.5))
            for ev in active:
                lv = ev.level(t, block_s, rng)
                if lv is not None:
                    e[ev.kind] = e.get(ev.kind, 0.0) + _db_to_e(lv)
            db = _e_to_db(sum(e.values())) + self.cal_error_db
            if db >= self.clip_db:
                db = self.clip_db
                clipped += 1
            levels.append(db)
            for k, v in e.items():
                win_e[k] = win_e.get(k, 0.0) + v
            win_n += 1
            if win_n == win_blocks and self.has_model:
                for lab, s in self._classify(win_e).items():
                    score_sums[lab] = score_sums.get(lab, 0.0) + s
                inferences += 1
                win_e, win_n = {}, 0

        self.events = [ev for ev in self.events if ev.end > t_end]

        lv = sorted(levels)
        laeq = _e_to_db(sum(_db_to_e(x) for x in lv) / len(lv)) if lv else 0.0
        total = sum(score_sums.values())
        sources = (
            {k: round(v / total, 3) for k, v in sorted(score_sums.items(), key=lambda kv: -kv[1])}
            if total > 0 else {}
        )
        flags: List[str] = [] if self.has_model else ["no_model"]
        if lv and clipped / len(lv) > 0.01:
            flags.append("clipping")
        if lost:
            flags.append("mic_dropout")
        return {
            "minute": utc_iso(t_start),
            "duration_s": round(duration_s, 1),
            "laeq_db": round(laeq, 1),
            "lmax_db": round(lv[-1], 1) if lv else 0.0,
            "l90_db": round(percentile(lv, 0.10), 1),
            "sources": sources,
            "valid_s": round(len(lv) * block_s, 1),
            "inferences": inferences,
            "flags": flags,
        }

    def _classify(self, energies: Dict[str, float]) -> Dict[str, float]:
        """Fake softmax output for one window: the loudest sounds dominate."""
        total = sum(energies.values()) or 1.0
        raw = {lab: 0.0 for lab in MODEL_LABELS}
        for kind, en in energies.items():
            w = (en / total) ** 2  # sharpen: the classifier hears the dominant sound
            for lab, p in CONFUSION[kind].items():
                raw[lab] += w * p
        rng = self.rng
        out = {lab: max(1e-4, v) * math.exp(rng.gauss(0, 0.45)) + 0.004 for lab, v in raw.items()}
        s = sum(out.values())
        return {lab: v / s for lab, v in out.items()}


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 50:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1

