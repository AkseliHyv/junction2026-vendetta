#!/usr/bin/env python3
"""
Night-Noise Box — Short-Window Loudness & Acoustic Label Reader (python/audio.py)
---------------------------------------------------------------------------------
Contract invariants (see /CONTRACT.md):
  - Produces `(level_db, label, confidence)` once per ~125 ms window (~8 Hz),
    alongside uncalibrated `raw_db` (`dBFS`) for live calibration.
  - Audio samples live ONLY in a fixed-size `array('h')` buffer allocated once in
    RAM and overwritten in place on every window.
  - Never writes audio to disk, never sends audio anywhere, never logs samples.
  - Default `CALIBRATION_OFFSET_DB` comes from `python/config.py` and can be
    updated live at runtime from the web page.

Pluggable Source Architecture:
  `LiveAudioSource` is a single small class with one method:
      read_window() -> tuple[float, str, float]   # (level_db, label, confidence)
  No live audio code is written yet (`TODO(BOARD-CHECK)`). On the UNO Q board,
  drop in whichever of the three candidate backends works cleanly:
    (a) `AudioClassification` brick for labels + shared `Microphone` for RMS level,
    (b) custom `Microphone` loop overwriting `_pcm_buf` + Edge Impulse runner, or
    (c) `AudioClassification` brick for labels + `mcu_level(db)` from the STM32.

Laptop check:
    python3 python/audio.py --self-test
    python3 python/audio.py --steps 16
"""

from __future__ import annotations

import argparse
import array
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from config import AUDIO_SOURCE, CALIBRATION_OFFSET_DB  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SAMPLE_RATE_HZ: int = 16000
WINDOW_HZ: int = 8
WINDOW_SAMPLES: int = SAMPLE_RATE_HZ // WINDOW_HZ  # 2000 int16 samples = 125 ms

# Baseline offset for interpreting nominal levels in DEFAULT_FAKE_SEQUENCE as dBFS
FAKE_NOMINAL_REF_OFFSET_DB: float = 94.0

MIN_DB: float = 0.0
MAX_DB: float = 120.0
MIN_RAW_DBFS: float = -120.0

ALLOWED_LABELS: Tuple[str, ...] = (
    "traffic",
    "construction",
    "music",
    "voices",
    "background",
)

# Realistic ~8 Hz fake night sequence (nominal dB at default 94.0 dB offset):
#   quiet -> traffic -> quiet -> voices -> quiet -> construction -> quiet -> music
DEFAULT_FAKE_SEQUENCE: List[Tuple[float, str, float]] = [
    # Quiet baseline (~35 dB, below END_THRESHOLD_DB=45)
    *[(34.8, "background", 0.94)] * 4,
    # Passing heavy vehicle ("traffic", 12 windows = 1.5 s >= 52 dB)
    (55.0, "traffic", 0.82),
    (59.5, "traffic", 0.87),
    (63.0, "traffic", 0.90),
    (65.4, "traffic", 0.93),
    (66.2, "traffic", 0.94),
    (65.0, "traffic", 0.92),
    (63.8, "traffic", 0.91),
    (62.4, "traffic", 0.90),
    (60.5, "traffic", 0.88),
    (58.8, "traffic", 0.86),
    (56.4, "traffic", 0.84),
    (54.2, "traffic", 0.82),
    # Quiet return (6 windows = 0.75 s <= 45 dB -> closes event cleanly)
    *[(35.0, "background", 0.94)] * 6,
    # Late-night street voices ("voices", 11 windows = 1.375 s >= 52 dB)
    (54.2, "voices", 0.82),
    (56.8, "voices", 0.86),
    (58.5, "voices", 0.89),
    (60.2, "voices", 0.91),
    (61.0, "voices", 0.92),
    (59.6, "voices", 0.90),
    (58.4, "voices", 0.88),
    (57.9, "voices", 0.88),
    (56.5, "voices", 0.86),
    (55.4, "voices", 0.85),
    (53.8, "voices", 0.83),
    # Quiet return
    *[(34.9, "background", 0.95)] * 6,
    # Utility / roadwork ("construction", 11 windows = 1.375 s >= 52 dB)
    (58.0, "construction", 0.85),
    (62.5, "construction", 0.89),
    (66.8, "construction", 0.92),
    (68.5, "construction", 0.94),
    (69.2, "construction", 0.95),
    (67.8, "construction", 0.93),
    (66.4, "construction", 0.92),
    (65.0, "construction", 0.91),
    (63.2, "construction", 0.90),
    (60.8, "construction", 0.88),
    (58.5, "construction", 0.86),
    # Quiet return
    *[(35.1, "background", 0.94)] * 6,
    # Distant bass / bar music ("music", 11 windows = 1.375 s >= 52 dB)
    (53.6, "music", 0.80),
    (56.4, "music", 0.85),
    (59.0, "music", 0.88),
    (60.8, "music", 0.90),
    (61.4, "music", 0.91),
    (60.2, "music", 0.89),
    (59.5, "music", 0.89),
    (58.2, "music", 0.87),
    (57.0, "music", 0.86),
    (55.8, "music", 0.85),
    (54.0, "music", 0.83),
    # Quiet return
    *[(35.0, "background", 0.95)] * 6,
]


@dataclass(frozen=True)
class AudioWindow:
    """Scalar-only window output; never carries PCM audio samples."""

    level_db: float
    label: str
    confidence: float
    raw_db: float = -59.0


class AudioWindowSource(Protocol):
    """Single-method interface shared by `LiveAudioSource` and `FakeAudioSource`."""

    def read_window(self) -> Tuple[float, str, float]:
        """Returns `(level_db, label, confidence)` for one ~125 ms window."""
        ...


def normalize_label(raw_label: str) -> str:
    """Maps Edge Impulse model class strings onto the 5 canonical contract labels."""
    cleaned = raw_label.strip().lower()
    if cleaned in ALLOWED_LABELS:
        return cleaned
    return "background"


def rms_norm_to_raw_dbfs(rms_norm: float) -> float:
    """Converts normalized RMS amplitude in [0.0, 1.0] to uncalibrated dBFS (<= 0.0)."""
    safe_rms = max(rms_norm, 1e-6)
    raw_dbfs = 20.0 * math.log10(min(1.0, safe_rms))
    return round(max(MIN_RAW_DBFS, min(0.0, raw_dbfs)), 1)


def rms_norm_to_db(rms_norm: float, calibration_offset_db: float = CALIBRATION_OFFSET_DB) -> float:
    """Converts normalized RMS amplitude in [0.0, 1.0] to calibrated dB."""
    raw_dbfs = rms_norm_to_raw_dbfs(rms_norm)
    db = raw_dbfs + float(calibration_offset_db)
    return round(max(MIN_DB, min(MAX_DB, db)), 1)


# ---------------------------------------------------------------------------
# Pluggable Sources: Live Board Stub vs. Laptop Fake Input
# ---------------------------------------------------------------------------
class LiveAudioSource:
    """
    Drop-in hardware source for the Arduino UNO Q.
    Exposes a single method `read_window() -> (level_db, label, confidence)` so
    whichever board pipeline passes physical testing can be plugged in here
    without touching `NightAudioMonitor` or `python/main.py`:
      (a) `AudioClassification` brick for labels + shared `Microphone` into `_pcm_buf`,
      (b) our own `Microphone` loop overwriting `_pcm_buf` + Edge Impulse `.eim` runner,
      (c) `AudioClassification` brick for labels + `mcu_level(db)` via `latest_mcu_db`.
    """

    def __init__(self, calibration_offset_db: float = CALIBRATION_OFFSET_DB) -> None:
        self.calibration_offset_db = float(calibration_offset_db)
        self._pcm_buf: array.array[int] = array.array("h", [0] * WINDOW_SAMPLES)
        self.latest_mcu_db: Optional[float] = None
        self.last_raw_db: float = -60.0

    def read_window(self) -> Tuple[float, str, float]:
        # TODO(BOARD-CHECK): Implement live UNO Q window acquisition after testing on
        # hardware which of (a) AudioClassification + shared Microphone,
        # (b) Microphone -> _pcm_buf -> Edge Impulse runner, or
        # (c) AudioClassification labels + optional `mcu_level(db)` from STM32
        # provides simultaneous level_db and label without writing audio to disk.
        raise NotImplementedError(
            "AUDIO_SOURCE='live' is not yet active: LiveAudioSource.read_window() "
            "requires physical UNO Q microphone + AudioClassification brick verification "
            "(see TODO(BOARD-CHECK) in python/audio.py). Run with --source fake meanwhile."
        )


class FakeAudioSource:
    """
    Deterministic fake source returning `(level_db, label, confidence)` per
    window while exercising the fixed-size overwritten `_pcm_buf`, raw `dBFS`
    measurement, and live `calibration_offset_db` adjustment.
    """

    def __init__(
        self,
        sequence: Optional[Sequence[Tuple[float, str, float]]] = None,
        calibration_offset_db: float = CALIBRATION_OFFSET_DB,
    ) -> None:
        self.calibration_offset_db = float(calibration_offset_db)
        # Single fixed-size PCM buffer in RAM (2000 x int16 = 4000 bytes),
        # overwritten in place every window; never resized, copied out, or logged.
        self._pcm_buf: array.array[int] = array.array("h", [0] * WINDOW_SAMPLES)
        self._items: List[Tuple[float, str, float]] = list(
            sequence if sequence is not None else DEFAULT_FAKE_SEQUENCE
        )
        if not self._items:
            self._items = list(DEFAULT_FAKE_SEQUENCE)
        self._idx: int = 0
        self.last_raw_db: float = -59.0

    def set_sequence(self, items: Iterable[Tuple[float, str, float]]) -> None:
        seq = list(items)
        if not seq:
            raise ValueError("sequence must contain at least one (level_db, label, confidence) tuple")
        self._items = seq
        self._idx = 0

    def _overwrite_buffer_for_nominal_db(self, nominal_db: float) -> None:
        clamped_nominal = max(MIN_DB, min(MAX_DB, float(nominal_db)))
        target_raw_dbfs = clamped_nominal - FAKE_NOMINAL_REF_OFFSET_DB
        rms_norm = math.pow(10.0, target_raw_dbfs / 20.0)
        peak_amp = min(32767.0, rms_norm * math.sqrt(2.0) * 32768.0)

        buf = self._pcm_buf
        two_pi_cycles_over_n = (2.0 * math.pi * 25.0) / WINDOW_SAMPLES
        for i in range(WINDOW_SAMPLES):
            buf[i] = int(round(peak_amp * math.sin(i * two_pi_cycles_over_n)))

    def _measure_buffer_raw_dbfs(self) -> float:
        buf = self._pcm_buf
        sum_sq = 0.0
        for sample in buf:
            sum_sq += float(sample * sample)
        rms_counts = math.sqrt(sum_sq / WINDOW_SAMPLES)
        return rms_norm_to_raw_dbfs(rms_counts / 32768.0)

    def read_window(self) -> Tuple[float, str, float]:
        nominal_db, raw_label, raw_conf = self._items[self._idx % len(self._items)]
        self._idx += 1
        self._overwrite_buffer_for_nominal_db(nominal_db)
        raw_dbfs = self._measure_buffer_raw_dbfs()
        self.last_raw_db = raw_dbfs
        level_db = round(max(MIN_DB, min(MAX_DB, raw_dbfs + self.calibration_offset_db)), 1)
        return level_db, raw_label, float(raw_conf)

    def zero_buffer(self) -> None:
        for i in range(WINDOW_SAMPLES):
            self._pcm_buf[i] = 0


class NightAudioMonitor:
    """
    Normalizes `(level_db, label, confidence)` + `raw_db` (`dBFS`) from either
    `FakeAudioSource` or `LiveAudioSource` into contract-compliant `AudioWindow` values.
    """

    def __init__(
        self,
        use_fake: Optional[bool] = None,
        fake_sequence: Optional[Sequence[Tuple[float, str, float]]] = None,
        calibration_offset_db: float = CALIBRATION_OFFSET_DB,
        source: Optional[AudioWindowSource] = None,
    ) -> None:
        resolved_fake = (AUDIO_SOURCE != "live") if use_fake is None else bool(use_fake)
        self.use_fake = resolved_fake
        self.source_name: str = "fake" if resolved_fake else "live"
        self.calibration_offset_db = float(calibration_offset_db)
        self._fake_source: Optional[FakeAudioSource] = None
        self._live_source: Optional[LiveAudioSource] = None

        if source is not None:
            self._source: AudioWindowSource = source
        elif self.use_fake:
            self._fake_source = FakeAudioSource(
                sequence=fake_sequence,
                calibration_offset_db=self.calibration_offset_db,
            )
            self._source = self._fake_source
        else:
            self._live_source = LiveAudioSource(calibration_offset_db=self.calibration_offset_db)
            self._source = self._live_source

    def set_calibration_offset(self, offset_db: float) -> None:
        """Updates `CALIBRATION_OFFSET_DB` at runtime without restarting."""
        val = round(float(offset_db), 1)
        self.calibration_offset_db = val
        if self._fake_source is not None:
            self._fake_source.calibration_offset_db = val
        if self._live_source is not None:
            self._live_source.calibration_offset_db = val

    def on_mcu_level(self, db: float) -> None:
        """Optional hook for candidate (c) (`mcu_level(db)` from STM32); unused by default."""
        if self._live_source is not None:
            self._live_source.latest_mcu_db = max(MIN_DB, min(MAX_DB, float(db)))

    def set_fake_sequence(self, items: Iterable[Tuple[float, str, float]]) -> None:
        if self._fake_source is None:
            raise RuntimeError("set_fake_sequence() is only valid when use_fake=True")
        self._fake_source.set_sequence(items)

    def read_window(self) -> AudioWindow:
        level_out, raw_label, raw_conf = self._source.read_window()
        level_db = round(max(MIN_DB, min(MAX_DB, float(level_out))), 1)
        raw_dbfs = getattr(self._source, "last_raw_db", None)
        if raw_dbfs is None:
            raw_db = round(level_db - self.calibration_offset_db, 1)
        else:
            raw_db = round(float(raw_dbfs), 1)
        label = normalize_label(raw_label)
        confidence = round(max(0.0, min(1.0, float(raw_conf))), 3)
        return AudioWindow(
            level_db=level_db,
            label=label,
            confidence=confidence,
            raw_db=raw_db,
        )

    def close(self) -> None:
        if self._fake_source is not None:
            self._fake_source.zero_buffer()
        if self._live_source is not None:
            for i in range(WINDOW_SAMPLES):
                self._live_source._pcm_buf[i] = 0


# ---------------------------------------------------------------------------
# Laptop Verification CLI & Self-Test
# ---------------------------------------------------------------------------
def run_self_test() -> None:
    print("=== Audio Check: python/audio.py (Config, Pluggable Source, dBFS & Fixed Buffer) ===")
    mon = NightAudioMonitor(use_fake=True)
    assert mon._fake_source is not None
    initial_buf_id = id(mon._fake_source._pcm_buf)
    initial_buf_len = len(mon._fake_source._pcm_buf)

    for _ in range(24):
        w = mon.read_window()
        assert id(mon._fake_source._pcm_buf) == initial_buf_id
        assert len(mon._fake_source._pcm_buf) == initial_buf_len == WINDOW_SAMPLES
        assert w.label in ALLOWED_LABELS
        assert 0.0 <= w.confidence <= 1.0
        assert w.raw_db <= 0.0
        assert abs((w.raw_db + mon.calibration_offset_db) - w.level_db) <= 0.2

    # Verify live runtime calibration shifts level_db while raw_db stays anchored to physical signal
    mon.set_fake_sequence([(50.0, "traffic", 0.90)])
    w_before = mon.read_window()
    assert abs(w_before.raw_db - (-44.0)) <= 0.2 and abs(w_before.level_db - 50.0) <= 0.2
    mon.set_calibration_offset(104.0)  # +10 dB higher offset
    w_after = mon.read_window()
    assert abs(w_after.raw_db - w_before.raw_db) <= 0.1
    assert abs(w_after.level_db - 60.0) <= 0.2

    # Verify custom single-method source drop-in compatibility
    class DummyDropInSource:
        def read_window(self) -> Tuple[float, str, float]:
            return (62.4, "construction", 0.91)

    drop_in_mon = NightAudioMonitor(use_fake=False, source=DummyDropInSource())
    dw = drop_in_mon.read_window()
    assert dw.level_db == 62.4 and dw.label == "construction" and dw.confidence == 0.91

    # Verify LiveAudioSource raises clear NotImplementedError until board test
    live_mon = NightAudioMonitor(use_fake=False)
    live_mon.on_mcu_level(51.2)
    assert live_mon._live_source is not None and live_mon._live_source.latest_mcu_db == 51.2
    try:
        live_mon.read_window()
        raise AssertionError("Expected LiveAudioSource.read_window() to raise NotImplementedError")
    except NotImplementedError as exc:
        assert "TODO(BOARD-CHECK)" in str(exc)

    mon.close()
    print("=== PASS: Single-method source, raw dBFS & runtime calibration verified ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Night-Noise Box Audio Window Monitor")
    parser.add_argument("--self-test", action="store_true", help="Run unit & buffer self-tests")
    parser.add_argument("--steps", type=int, default=12, help="Number of ~125 ms fake windows to print")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    mon = NightAudioMonitor(use_fake=True)
    for idx in range(args.steps):
        win = mon.read_window()
        print(
            f"[window {idx + 1:02d}] raw={win.raw_db:6.1f} dBFS  "
            f"level={win.level_db:5.1f} dB  label={win.label:<12s}  conf={win.confidence:.2f}"
        )
    mon.close()


if __name__ == "__main__":
    main()
