#!/usr/bin/env python3
"""
Night-Noise Box — Single Tunable Configuration File (python/config.py)
----------------------------------------------------------------------
Every tunable value for the Linux coordinator, audio monitor, and SQLite
retention lives in this one module.

  - `AUDIO_SOURCE`: "fake" | "live" (default "fake"). Can be overridden at
    startup via `python3 python/main.py --source fake|live`.
  - `CALIBRATION_OFFSET_DB`: Default offset added to raw dBFS (`0 dBFS` ->
    `CALIBRATION_OFFSET_DB`). Overridden automatically on startup when a saved
    `<db>.calibration.json` file exists next to the SQLite database.
  - `GRID_CELL`: Single configured grid cell ID for this physical UNO Q box.
  - `RETENTION_DAYS`: Days of event history retained in SQLite.
  - `DEFAULT_PROFILE`: Startup threshold profile sent to the microcontroller.
  - `PROFILES`: Named threshold & hold-time profiles (same dB/ms units as
    `sketch/sketch.ino`), switchable live from the web page without reflashing.
"""

from __future__ import annotations

from typing import Dict, TypedDict


class ThresholdProfile(TypedDict):
    start_db: float
    end_db: float
    start_hold_ms: int
    end_hold_ms: int


AUDIO_SOURCE: str = "fake"  # "fake" | "live"
CALIBRATION_OFFSET_DB: float = 94.0
GRID_CELL: str = "B2"
RETENTION_DAYS: int = 14
DEFAULT_PROFILE: str = "night"

PROFILES: Dict[str, ThresholdProfile] = {
    "night": {
        "start_db": 52.0,
        "end_db": 45.0,
        "start_hold_ms": 375,
        "end_hold_ms": 625,
    },
    "demo": {
        "start_db": 70.0,
        "end_db": 62.0,
        "start_hold_ms": 375,
        "end_hold_ms": 625,
    },
}

# ---------------------------------------------------------------------------
# Noise Map upload (server/server.py). Event records (scalars only, never
# audio) are POSTed as JSON when an event ends. Set UPLOAD_URL = "" to keep
# the box fully offline. Override at startup with `--upload-url`.
# ---------------------------------------------------------------------------
UPLOAD_URL: str = "http://192.168.127.235:8080/api/events"
DEVICE_ID: str = "box-01"
DEVICE_LAT: float = 60.15675541136981   # Where this box sits — set to your real location
DEVICE_LON: float = 24.630079696276763

# ---------------------------------------------------------------------------
# Periodic analysis mode (python/analysis.py)
# ---------------------------------------------------------------------------
# "analysis": listen to the mic for ANALYSIS_SECONDS, upload one v1 summary
#             (LAeq / Lmax / L90 / sound sources) to READINGS_URL, repeat.
# "events":   the original MCU-driven event pipeline + local web page.
# App Lab starts main.py without arguments, so this picks what runs on the board.
RUN_MODE: str = "analysis"            # "analysis" | "events"

MIC_SOURCE: str = "alsa"              # "alsa" (USB camera mic via arecord) | "synthetic" (laptop test)
MIC_DEVICE: str = "auto"              # "auto" = first USB/camera capture card, or e.g. "plughw:1,0"
SAMPLE_RATE_HZ: int = 16000           # Overridden by the Edge Impulse model's own frequency when loaded

ANALYSIS_SECONDS: float = 10.0        # Length of one analysis (raise later, e.g. 900 for 15 min)
READING_SECONDS: float = 10.0         # One reading per this many seconds inside an analysis (60 = per minute)

READINGS_URL: str = "http://192.168.127.235:8080/api/readings"  # "" = don't upload, just print

# Edge Impulse Linux model (.eim). Relative paths are resolved from the app
# folder. Until the file exists the analysis runs with a placeholder
# classifier and uploads empty `sources` + a "no_model" flag.
EI_MODEL_PATH: str = "models/noise-classifier.eim"
EI_MIN_CONFIDENCE: float = 0.0        # Ignore inference windows whose top score is below this
# Model label -> name uploaded in `sources`. Unlisted labels pass through
# unchanged; map a label to "" to drop it (e.g. silence / noise-floor classes).
EI_LABEL_MAP: Dict[str, str] = {
    # "car": "traffic",
    # "speech": "voices",
    # "silence": "",
}
