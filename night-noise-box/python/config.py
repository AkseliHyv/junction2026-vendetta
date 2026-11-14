"""Settings for the box. App Lab runs main.py without arguments, so this file is
how you configure the board."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, TypedDict


def _env_file(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines from a .env file, if it exists."""
    values: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


# --- Identity and server ---

DEVICE_ID: str = "box-01"
DEVICE_LAT: float = 60.15675541136981  # where the box is; this is where it shows up on the map
DEVICE_LON: float = 24.630079696276763

READINGS_URL: str = "https://70-34-222-243.sslip.io/api/readings"  # "" = print only, don't upload
UPLOAD_URL: str = "https://70-34-222-243.sslip.io/api/events"  # events mode only; "" = offline

# Shared key the server checks on uploads. Not kept in this file: set the
# NOISE_MAP_KEY environment variable, or put NOISE_MAP_KEY=... in
# night-noise-box/.env (ignored by git, copied to the board by deploy.sh).
NOISE_MAP_KEY: str = os.environ.get("NOISE_MAP_KEY") or _env_file(Path(__file__).resolve().parent.parent / ".env").get(
    "NOISE_MAP_KEY", ""
)

# "analysis": measure for ANALYSIS_SECONDS, upload a summary, repeat.
# "events":   the older pipeline where the STM32 detects noise events and the
#             board serves its own heatmap page on port 7000.
RUN_MODE: str = "analysis"

# --- Analysis mode ---

MIC_SOURCE: str = "alsa"  # "alsa" (USB mic via arecord) or "synthetic" (laptop testing)
MIC_DEVICE: str = "auto"  # "auto" picks the first USB capture card, or e.g. "plughw:1,0"
SAMPLE_RATE_HZ: int = 16000  # replaced by the model's own rate when a model is loaded

ANALYSIS_SECONDS: float = 10.0  # one upload per this many seconds
READING_SECONDS: float = 10.0  # length of each reading inside an upload (60 = per minute)

# Edge Impulse model. Relative paths are resolved from the app folder. Without a
# model, loudness is still uploaded but `sources` is empty and flagged "no_model".
EI_MODEL_PATH: str = "models/noise-classifier.eim"
EI_MIN_CONFIDENCE: float = 0.0  # skip classifier windows whose top score is below this
# Rename model labels before upload. Unlisted labels pass through; "" drops a label.
EI_LABEL_MAP: Dict[str, str] = {
    # "car": "traffic",
    # "speech": "voices",
    # "silence": "",
}

# Added to raw dBFS to get dB SPL. Replaced by the saved value after calibrating
# from the local page (events mode).
CALIBRATION_OFFSET_DB: float = 94.0

# --- Events mode ---

AUDIO_SOURCE: str = "fake"  # "fake" or "live"; overridden by --source
GRID_CELL: str = "B2"  # this box's cell on the local area heatmap
RETENTION_DAYS: int = 14
DEFAULT_PROFILE: str = "night"


class ThresholdProfile(TypedDict):
    start_db: float
    end_db: float
    start_hold_ms: int
    end_hold_ms: int


# Thresholds the STM32 uses to open and close an event. Switchable from the local page.
PROFILES: Dict[str, ThresholdProfile] = {
    "night": {"start_db": 52.0, "end_db": 45.0, "start_hold_ms": 375, "end_hold_ms": 625},
    "demo": {"start_db": 70.0, "end_db": 62.0, "start_hold_ms": 375, "end_hold_ms": 625},
}
