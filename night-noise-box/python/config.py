"""Settings for the box. App Lab runs main.py without arguments, so this file is
how you configure the board."""

from __future__ import annotations

from typing import Dict, TypedDict

# --- Identity and server ---

DEVICE_ID: str = "box-01"
DEVICE_LAT: float = 60.15675541136981  # where the box is; this is where it shows up on the map
DEVICE_LON: float = 24.630079696276763

READINGS_URL: str = "http://192.168.127.235:8080/api/readings"  # "" = print only, don't upload
UPLOAD_URL: str = "http://192.168.127.235:8080/api/events"  # events mode only; "" = offline

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
