#!/usr/bin/env python3
"""Entry point on the board. App Lab runs this file.

RUN_MODE in config.py picks what runs:

  analysis  Measure noise with the USB mic and upload a summary to the map
            server every ANALYSIS_SECONDS (see analysis.py). The default.

  events    The older pipeline. Linux sends loudness levels to the STM32,
            the STM32 decides when a noise event starts and ends, and Linux
            stores each event in SQLite and serves a heatmap page on port 7000.
            Events can also be uploaded to the map server's /api/events.

    python3 python/main.py                               # whatever RUN_MODE says
    python3 python/main.py --mode events --source fake   # events page on a laptop
    python3 python/main.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from audio import MAX_DB, MIN_DB, WINDOW_HZ, AudioWindow, NightAudioMonitor  # noqa: E402
from config import (  # noqa: E402
    AUDIO_SOURCE,
    CALIBRATION_OFFSET_DB,
    DEFAULT_PROFILE,
    DEVICE_ID,
    DEVICE_LAT,
    DEVICE_LON,
    GRID_CELL,
    PROFILES,
    READINGS_URL,
    RETENTION_DAYS,
    RUN_MODE,
    UPLOAD_URL,
)
from store import CSV_COLUMNS, NoiseStore  # noqa: E402
from uploader import EventUploader  # noqa: E402

WINDOW_INTERVAL_SEC: float = 1.0 / float(WINDOW_HZ)
WOKE_LOOKBACK_SEC: float = 600.0  # a "woke me" press attaches to an event that ended this recently
TAMPER_LATCH_SEC: float = 60.0  # events starting this soon after a tamper are flagged too
CALIBRATION_AVG_SEC: float = 3.0
MCU_HEARTBEAT_TIMEOUT_SEC: float = 3.0
DIAGNOSTICS_INTERVAL_SEC: float = 1.0

# The arduino packages only exist on the board. Without them we run a local
# HTTP server and simulate the STM32.
try:
    from arduino.app_utils import App, Bridge  # type: ignore
    from arduino.app_bricks.web_ui import WebUI  # type: ignore

    ON_UNO_Q = True
except ImportError:
    ON_UNO_Q = False
    App = None  # type: ignore
    Bridge = None  # type: ignore
    WebUI = None  # type: ignore


def default_calibration_path_for_db(db_path: Path | str) -> Optional[Path]:
    """<db name>.calibration.json next to the database, or None for :memory:."""
    db_str = str(db_path)
    if db_str == ":memory:":
        return None
    p = Path(db_str).resolve()
    return p.with_name(f"{p.stem}.calibration.json")


def load_saved_calibration(cal_path: Optional[Path]) -> Optional[Tuple[float, Optional[str]]]:
    if cal_path is None or not cal_path.exists():
        return None
    try:
        data = json.loads(cal_path.read_text(encoding="utf-8"))
        offset = round(float(data["offset_db"]), 1)
        cal_at = data.get("calibrated_at")
        return offset, (str(cal_at) if cal_at else None)
    except Exception:
        return None


def save_calibration_atomic(cal_path: Optional[Path], offset_db: float, calibrated_at: str) -> None:
    if cal_path is None:
        return
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cal_path.with_suffix(cal_path.suffix + ".tmp")
    payload = {
        "offset_db": round(float(offset_db), 1),
        "calibrated_at": str(calibrated_at),
    }
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, cal_path)


class LaptopDevWebUI:
    """Stand-in for the WebUI brick on a laptop. Pushes messages to the page over SSE."""

    def __init__(self, log_events: bool = True) -> None:
        self.handlers: Dict[str, Callable[..., None]] = {}
        self.sent_log: List[tuple[str, Dict[str, Any]]] = []
        self.sse_clients: List[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self.log_events = log_events

    def send_message(self, event: str, payload: Dict[str, Any]) -> None:
        if self.log_events and event not in ("live", "diagnostics"):
            print(f"[WebUI TX] {event}: {json.dumps(payload)}")
        with self._lock:
            self.sent_log.append((event, payload))
            if len(self.sent_log) > 400:
                self.sent_log = self.sent_log[-400:]
            dead: List[queue.Queue[str]] = []
            frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            for q in self.sse_clients:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.sse_clients.remove(q)

    def on_message(self, event: str, callback: Callable[..., None]) -> None:
        self.handlers[event] = callback

    def dispatch_from_client(self, event: str, payload: Dict[str, Any]) -> None:
        cb = self.handlers.get(event)
        if cb is not None:
            cb("laptop-client", payload)


class BridgeSink:
    """Sends level, label and thresholds to the STM32, or to the simulator on a laptop."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.last_label_sent: Optional[str] = None
        self.level_tx_count: int = 0
        self.label_tx_count: int = 0
        self.thresholds_tx_log: List[Tuple[float, float, int, int]] = []
        self._sim: Any = None
        self._sim_clock_ms: int = 0

    def attach_laptop_simulator(self, sim: Any) -> None:
        self._sim = sim

    def notify_level(self, db: float, sim_now_ms: Optional[int] = None) -> None:
        val = round(float(db), 1)
        self.level_tx_count += 1
        if ON_UNO_Q and Bridge is not None:
            Bridge.notify("level", val)
            return
        if self.verbose:
            print(f"[Bridge TX -> MCU] level({val:.1f})")
        if self._sim is not None:
            if sim_now_ms is not None:
                self._sim_clock_ms = int(sim_now_ms)
            else:
                self._sim_clock_ms += int(WINDOW_INTERVAL_SEC * 1000)
            self._sim.rx_level(self._sim_clock_ms, val)

    def notify_label_if_changed(
        self,
        name: str,
        confidence: float,
        sim_now_ms: Optional[int] = None,
    ) -> None:
        if name == self.last_label_sent:
            return
        self.last_label_sent = name
        conf = round(float(confidence), 3)
        self.label_tx_count += 1
        if ON_UNO_Q and Bridge is not None:
            Bridge.notify("label", name, conf)
            return
        if self.verbose:
            print(f"[Bridge TX -> MCU] label({name!r}, {conf:.3f})")
        if self._sim is not None:
            t_ms = int(sim_now_ms) if sim_now_ms is not None else self._sim_clock_ms
            self._sim.rx_label(t_ms, name, conf)

    def notify_set_thresholds(
        self,
        start_db: float,
        end_db: float,
        start_hold_ms: int,
        end_hold_ms: int,
        sim_now_ms: Optional[int] = None,
    ) -> None:
        s_db = round(float(start_db), 1)
        e_db = round(float(end_db), 1)
        s_hold = int(start_hold_ms)
        e_hold = int(end_hold_ms)
        self.thresholds_tx_log.append((s_db, e_db, s_hold, e_hold))
        if ON_UNO_Q and Bridge is not None:
            Bridge.notify("set_thresholds", s_db, e_db, s_hold, e_hold)
            return
        if self.verbose:
            print(f"[Bridge TX -> MCU] set_thresholds({s_db}, {e_db}, {s_hold}, {e_hold})")
        if self._sim is not None:
            t_ms = int(sim_now_ms) if sim_now_ms is not None else self._sim_clock_ms
            self._sim.rx_set_thresholds(t_ms, s_db, e_db, s_hold, e_hold)


class NightNoiseCoordinator:
    """Events mode: ties audio, the STM32, the database and the page together."""

    def __init__(
        self,
        audio: NightAudioMonitor,
        store: NoiseStore,
        ui: Any,
        bridge: BridgeSink,
        calibration_path: Optional[Path] = None,
        default_profile: str = DEFAULT_PROFILE,
        clock_fn: Optional[Callable[[], datetime]] = None,
        mono_fn: Optional[Callable[[], float]] = None,
        uploader: Optional[EventUploader] = None,
    ) -> None:
        self.audio = audio
        self.uploader = uploader
        self.store = store
        self.ui = ui
        self.bridge = bridge
        self.calibration_path = (
            calibration_path
            if calibration_path is not None
            else default_calibration_path_for_db(store.db_path)
        )
        self._clock_fn = clock_fn if clock_fn is not None else datetime.now
        self._mono_fn = mono_fn if mono_fn is not None else time.monotonic
        self._lock = threading.RLock()

        self.calibration_offset_db: float = round(float(audio.calibration_offset_db), 1)
        self.calibrated_at: Optional[str] = None
        saved_cal = load_saved_calibration(self.calibration_path)
        if saved_cal is not None:
            self.calibration_offset_db, self.calibrated_at = saved_cal
            self.audio.set_calibration_offset(self.calibration_offset_db)

        self.active_profile: str = (
            default_profile if default_profile in PROFILES else "night"
        )

        self.current_raw_db: float = -59.0
        self.current_level_db: float = round(
            max(MIN_DB, min(MAX_DB, self.current_raw_db + self.calibration_offset_db)), 1
        )
        self.current_label: str = "background"
        self.current_confidence: float = 0.90
        self._raw_db_ring: Deque[Tuple[float, float]] = deque()
        self._last_level_mono: Optional[float] = None
        self._last_heartbeat_mono: Optional[float] = None
        self._last_diag_mono: float = -999.0

        # The event currently open, between the STM32's event_start and event_end.
        self.event_open: bool = False
        self._open_start_iso: str = ""
        self._open_levels: List[float] = []
        self._label_durations_sec: Dict[str, float] = {}
        self._label_conf_weighted: Dict[str, float] = {}
        self._last_slice_mono: float = 0.0
        self._open_woke_me: bool = False
        self._open_tamper: bool = False

        self._tamper_until_mono: Optional[float] = None

        self._register_ui_handlers()

    def _wrap_ui_cb(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[..., None]:
        def _inner(*args: Any) -> None:
            raw = args[-1] if args else {}
            if isinstance(raw, str):
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
            elif isinstance(raw, dict):
                payload = raw
            else:
                payload = {}
            fn(payload)

        return _inner

    def _register_ui_handlers(self) -> None:
        self.ui.on_message("request_state", self._wrap_ui_cb(self.handle_request_state))
        self.ui.on_message("set_profile", self._wrap_ui_cb(self.handle_set_profile))
        self.ui.on_message("calibrate", self._wrap_ui_cb(self.handle_calibrate))
        self.ui.on_message("export_csv", self._wrap_ui_cb(self.handle_export_csv))

    def sync_startup_thresholds(self, sim_now_ms: Optional[int] = None) -> None:
        """Sends the active threshold profile to the STM32."""
        with self._lock:
            prof = PROFILES[self.active_profile]
            self.bridge.notify_set_thresholds(
                prof["start_db"],
                prof["end_db"],
                prof["start_hold_ms"],
                prof["end_hold_ms"],
                sim_now_ms=sim_now_ms,
            )

    def _now_iso(self) -> str:
        return self._clock_fn().strftime("%Y-%m-%dT%H:%M:%S")

    def _now_epoch(self) -> float:
        return self._clock_fn().timestamp()

    def _is_tamper_latched(self, now_mono: float) -> bool:
        return self._tamper_until_mono is not None and now_mono <= self._tamper_until_mono

    def _accumulate_open_label_slice(self, now_mono: float) -> None:
        if not self.event_open:
            return
        dt = max(0.0, now_mono - self._last_slice_mono)
        self._last_slice_mono = now_mono
        if dt <= 0.0:
            return
        lbl = self.current_label
        self._label_durations_sec[lbl] = self._label_durations_sec.get(lbl, 0.0) + dt
        self._label_conf_weighted[lbl] = (
            self._label_conf_weighted.get(lbl, 0.0) + self.current_confidence * dt
        )

    def _prune_raw_ring(self, now_mono: float) -> None:
        cutoff = now_mono - CALIBRATION_AVG_SEC + 1e-9
        while self._raw_db_ring and self._raw_db_ring[0][0] <= cutoff:
            self._raw_db_ring.popleft()

    def mean_raw_db_last_3s(self, now_mono: Optional[float] = None) -> float:
        ref = now_mono if now_mono is not None else self._mono_fn()
        with self._lock:
            self._prune_raw_ring(ref)
            if not self._raw_db_ring:
                return self.current_raw_db
            vals = [v for _, v in self._raw_db_ring]
            return sum(vals) / len(vals)

    # --- Messages to the page ---
    def broadcast_live(self) -> None:
        self.ui.send_message(
            "live",
            {
                "level": round(self.current_level_db, 1),
                "raw_db": round(self.current_raw_db, 1),
                "label": self.current_label,
            },
        )

    def broadcast_heatmap(self) -> None:
        self.ui.send_message("heatmap", self.store.build_heatmap_data())

    def build_diagnostics_payload(self) -> Dict[str, Any]:
        now_mono = self._mono_fn()
        now_dt = self._clock_fn()
        sec_since_level: Optional[float] = (
            round(max(0.0, now_mono - self._last_level_mono), 1)
            if self._last_level_mono is not None
            else None
        )
        mcu_ok = (
            self._last_heartbeat_mono is not None
            and (now_mono - self._last_heartbeat_mono) <= MCU_HEARTBEAT_TIMEOUT_SEC
        )
        return {
            "audio_source": self.audio.source_name,
            "active_profile": self.active_profile,
            "profiles": PROFILES,
            "calibration_offset_db": round(self.calibration_offset_db, 1),
            "calibrated_at": self.calibrated_at,
            "sec_since_last_level": sec_since_level,
            "mcu_responding": bool(mcu_ok),
            "events_stored": self.store.count_events(),
            "db_size_bytes": self.store.db_size_bytes(),
            "board_time_iso": now_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "board_epoch_ms": int(round(now_dt.timestamp() * 1000.0)),
        }

    def broadcast_diagnostics(self) -> None:
        with self._lock:
            self._last_diag_mono = self._mono_fn()
            self.ui.send_message("diagnostics", self.build_diagnostics_payload())

    # --- Messages from the page ---
    def handle_request_state(self, _payload: Dict[str, Any]) -> None:
        with self._lock:
            self.broadcast_live()
            self.broadcast_heatmap()
            self.broadcast_diagnostics()

    def handle_set_profile(self, payload: Dict[str, Any]) -> None:
        requested = str(payload.get("profile", "")).strip().lower()
        if requested not in PROFILES:
            return
        with self._lock:
            self.active_profile = requested
            prof = PROFILES[requested]
            self.bridge.notify_set_thresholds(
                prof["start_db"],
                prof["end_db"],
                prof["start_hold_ms"],
                prof["end_hold_ms"],
            )
            self.broadcast_diagnostics()

    def handle_calibrate(self, payload: Dict[str, Any]) -> None:
        raw_phone = payload.get("phone_db")
        if raw_phone is None:
            return
        try:
            phone_db = float(raw_phone)
        except (TypeError, ValueError):
            return
        if not (10.0 <= phone_db <= 130.0):
            return

        now_mono = self._mono_fn()
        with self._lock:
            avg_raw = self.mean_raw_db_last_3s(now_mono)
            new_offset = round(phone_db - avg_raw, 1)
            self.calibration_offset_db = new_offset
            self.calibrated_at = self._now_iso()
            self.audio.set_calibration_offset(new_offset)
            self.current_level_db = round(
                max(MIN_DB, min(MAX_DB, self.current_raw_db + new_offset)), 1
            )
            save_calibration_atomic(
                self.calibration_path,
                self.calibration_offset_db,
                self.calibrated_at,
            )
            self.broadcast_live()
            self.broadcast_diagnostics()

    def handle_export_csv(self, _payload: Dict[str, Any]) -> None:
        with self._lock:
            date_tag = self._clock_fn().strftime("%Y%m%d-%H%M%S")
            self.ui.send_message(
                "csv_export",
                {
                    "filename": f"night-noise-events-{date_tag}.csv",
                    "csv": self.store.export_csv_text(),
                },
            )

    # --- Audio, called 8 times a second ---
    def step_once(
        self,
        injected_window: Optional[AudioWindow] = None,
        sim_now_ms: Optional[int] = None,
    ) -> AudioWindow:
        win = injected_window if injected_window is not None else self.audio.read_window()
        now_mono = self._mono_fn()

        with self._lock:
            self._accumulate_open_label_slice(now_mono)

            self.current_raw_db = win.raw_db
            self.current_level_db = win.level_db
            self.current_label = win.label
            self.current_confidence = win.confidence
            self._last_level_mono = now_mono
            self._raw_db_ring.append((now_mono, win.raw_db))
            self._prune_raw_ring(now_mono)

            self.bridge.notify_label_if_changed(win.label, win.confidence, sim_now_ms=sim_now_ms)
            self.bridge.notify_level(win.level_db, sim_now_ms=sim_now_ms)

            if self.event_open:
                self._open_levels.append(win.level_db)

            self.broadcast_live()
            if (now_mono - self._last_diag_mono) >= DIAGNOSTICS_INTERVAL_SEC:
                self.broadcast_diagnostics()
            return win

    # --- Messages from the STM32 ---
    def on_mcu_event_start(self) -> None:
        now_mono = self._mono_fn()
        with self._lock:
            if self.event_open:
                return
            self.event_open = True
            self._open_start_iso = self._now_iso()
            self._open_levels = [self.current_level_db]
            self._label_durations_sec = {}
            self._label_conf_weighted = {}
            self._last_slice_mono = now_mono
            self._open_woke_me = False
            self._open_tamper = self._is_tamper_latched(now_mono)

    def on_mcu_event_end(self, duration_ms: int) -> None:
        now_mono = self._mono_fn()
        with self._lock:
            if not self.event_open:
                return
            self._accumulate_open_label_slice(now_mono)

            if self._label_durations_sec:
                best_label = max(
                    self._label_durations_sec.items(),
                    key=lambda kv: kv[1],
                )[0]
                dur_total = self._label_durations_sec[best_label]
                best_conf = (
                    round(self._label_conf_weighted[best_label] / dur_total, 3)
                    if dur_total > 0.0
                    else round(self.current_confidence, 3)
                )
            else:
                best_label = self.current_label
                best_conf = round(self.current_confidence, 3)

            levels = self._open_levels if self._open_levels else [self.current_level_db]
            avg_lvl = round(sum(levels) / len(levels), 1)
            peak_lvl = round(max(levels), 1)

            self.event_open = False
            record = self.store.insert_event(
                start_time=self._open_start_iso,
                duration_ms=max(0, int(duration_ms)),
                label=best_label,
                confidence=best_conf,
                avg_level=avg_lvl,
                peak_level=peak_lvl,
                woke_me=self._open_woke_me,
                tamper=self._open_tamper,
                grid_cell=GRID_CELL,
                end_epoch=self._now_epoch(),
                now_dt=self._clock_fn(),
            )
            self.ui.send_message("event", record)
            self._upload(record)
            self.broadcast_heatmap()
            self.broadcast_diagnostics()

    def on_mcu_woke_me(self) -> None:
        now_mono = self._mono_fn()
        with self._lock:
            if self.event_open:
                self._open_woke_me = True
                return

            updated = self.store.mark_latest_recent_woke_me(
                now_epoch=self._now_epoch(),
                max_age_sec=WOKE_LOOKBACK_SEC,
            )
            if updated is not None:
                self.ui.send_message("event", updated)
                self._upload(updated)
                self.broadcast_heatmap()
                self.broadcast_diagnostics()
                return

            standalone = self.store.insert_event(
                start_time=self._now_iso(),
                duration_ms=0,
                label="unknown",
                confidence=0.0,
                avg_level=round(self.current_level_db, 1),
                peak_level=round(self.current_level_db, 1),
                woke_me=True,
                tamper=self._is_tamper_latched(now_mono),
                grid_cell=GRID_CELL,
                end_epoch=self._now_epoch(),
                now_dt=self._clock_fn(),
            )
            self.ui.send_message("event", standalone)
            self._upload(standalone)
            self.broadcast_heatmap()
            self.broadcast_diagnostics()

    def _upload(self, record: Dict[str, Any]) -> None:
        if self.uploader is not None:
            self.uploader.submit(record)

    def on_mcu_tamper(self) -> None:
        now_mono = self._mono_fn()
        with self._lock:
            self._tamper_until_mono = now_mono + TAMPER_LATCH_SEC
            if self.event_open:
                self._open_tamper = True

    def on_mcu_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat_mono = self._mono_fn()

    def on_mcu_level(self, db: float) -> None:
        """Level from a mic wired to the STM32. Not used with the USB mic."""
        self.audio.on_mcu_level(float(db))

    def on_mcu_knob(self, _value: int) -> None:
        """The sketch reports knob turns. Ignored."""
        return


def run_self_test() -> None:
    from sim_imu_replay import SketchNoiseSimulator

    sim_state = {"mono": 100.0, "dt": datetime(2026, 11, 14, 23, 10, 0), "ms": 0}

    def fake_clock() -> datetime:
        return sim_state["dt"]

    def fake_mono() -> float:
        return sim_state["mono"]

    def advance_ms(delta_ms: int) -> None:
        sim_state["ms"] += delta_ms
        sim_state["mono"] += delta_ms / 1000.0
        sim_state["dt"] = sim_state["dt"] + timedelta(milliseconds=delta_ms)

    with tempfile.TemporaryDirectory(prefix="night_noise_test_") as tmp_dir:
        db_file = Path(tmp_dir) / "test_noise.sqlite3"
        cal_file = default_calibration_path_for_db(db_file)
        assert cal_file is not None

        audio = NightAudioMonitor(use_fake=True, calibration_offset_db=CALIBRATION_OFFSET_DB)
        store = NoiseStore(db_path=db_file, retention_days=RETENTION_DAYS, grid_cell=GRID_CELL)
        ui = LaptopDevWebUI(log_events=False)
        bridge = BridgeSink(verbose=False)
        coord = NightNoiseCoordinator(
            audio=audio,
            store=store,
            ui=ui,
            bridge=bridge,
            calibration_path=cal_file,
            clock_fn=fake_clock,
            mono_fn=fake_mono,
        )
        mcu_sim = SketchNoiseSimulator(
            on_event_start=coord.on_mcu_event_start,
            on_event_end=coord.on_mcu_event_end,
            on_woke_me=coord.on_mcu_woke_me,
            on_tamper=coord.on_mcu_tamper,
            on_heartbeat=coord.on_mcu_heartbeat,
        )
        bridge.attach_laptop_simulator(mcu_sim)
        coord.sync_startup_thresholds(sim_now_ms=sim_state["ms"])
        assert mcu_sim.active_start_db == 52.0 and mcu_sim.active_end_db == 45.0

        # "Woke me" with no recent event creates a standalone "unknown" event.
        mcu_sim.press_woke_button(sim_state["ms"])
        ev_msgs = [p for ev, p in ui.sent_log if ev == "event"]
        assert len(ev_msgs) == 1 and ev_msgs[0]["label"] == "unknown" and ev_msgs[0]["woke_me"] is True

        # A loud event with mixed labels under the night profile (52 / 45 dB).
        event_windows = [
            (35.0, "background", 0.95),
            (56.0, "voices", 0.80),
            (58.0, "voices", 0.84),
            (60.0, "voices", 0.82),
            (62.0, "traffic", 0.86),
            (65.0, "traffic", 0.88),
            (67.0, "traffic", 0.90),
            (68.0, "traffic", 0.94),
            (66.0, "traffic", 0.92),
            (65.0, "traffic", 0.90),
            (64.0, "traffic", 0.90),
            (63.0, "traffic", 0.90),
            (59.0, "voices", 0.75),
            (41.0, "background", 0.92),
            (38.0, "background", 0.94),
            (36.0, "background", 0.95),
            (35.0, "background", 0.95),
            (35.0, "background", 0.95),
            (35.0, "background", 0.95),
        ]
        audio.set_fake_sequence(event_windows)
        for _ in range(len(event_windows)):
            advance_ms(125)
            coord.step_once(sim_now_ms=sim_state["ms"])

        ev_msgs = [p for ev, p in ui.sent_log if ev == "event"]
        assert len(ev_msgs) == 2
        traffic_ev = ev_msgs[-1]
        assert traffic_ev["label"] == "traffic"

        # Under the demo profile (70 / 62 dB), 62 dB must not start an event.
        ui.dispatch_from_client("set_profile", {"profile": "demo"})
        assert coord.active_profile == "demo"
        assert mcu_sim.active_start_db == 70.0 and mcu_sim.active_end_db == 62.0
        audio.set_fake_sequence([(62.0, "music", 0.90)] * 8)
        for _ in range(8):
            advance_ms(125)
            coord.step_once(sim_now_ms=sim_state["ms"])
        assert coord.event_open is False
        assert len([p for ev, p in ui.sent_log if ev == "event"]) == 2

        ui.dispatch_from_client("set_profile", {"profile": "night"})
        assert mcu_sim.active_start_db == 52.0

        # Calibrate: 3 s of -54 dBFS and a phone reading of 50 dB gives an offset of 104 dB.
        audio.set_fake_sequence([(40.0, "background", 0.95)])
        for _ in range(24):
            advance_ms(125)
            coord.step_once(sim_now_ms=sim_state["ms"])
        ui.dispatch_from_client("calibrate", {"phone_db": 50.0})
        assert abs(coord.calibration_offset_db - 104.0) <= 0.2
        assert cal_file.exists(), "Calibration JSON file must be written next to SQLite DB"

        diag_msgs = [p for ev, p in ui.sent_log if ev == "diagnostics"]
        assert diag_msgs, "Expected diagnostics messages"
        latest_diag = diag_msgs[-1]
        assert latest_diag["mcu_responding"] is True
        assert latest_diag["events_stored"] == 2
        assert latest_diag["db_size_bytes"] > 0
        assert latest_diag["calibrated_at"] is not None

        ui.dispatch_from_client("export_csv", {})
        csv_msgs = [p for ev, p in ui.sent_log if ev == "csv_export"]
        assert len(csv_msgs) == 1
        rows = list(csv.reader(io.StringIO(csv_msgs[0]["csv"])))
        assert tuple(rows[0]) == CSV_COLUMNS
        assert len(rows) == 3  # header + 2 stored events

        store.close()
        audio.close()

        # The calibration survives a restart.
        audio_restarted = NightAudioMonitor(use_fake=True, calibration_offset_db=CALIBRATION_OFFSET_DB)
        store_restarted = NoiseStore(db_path=db_file, retention_days=RETENTION_DAYS, grid_cell=GRID_CELL)
        ui_restarted = LaptopDevWebUI(log_events=False)
        coord_restarted = NightNoiseCoordinator(
            audio=audio_restarted,
            store=store_restarted,
            ui=ui_restarted,
            bridge=BridgeSink(verbose=False),
            calibration_path=cal_file,
            clock_fn=fake_clock,
            mono_fn=fake_mono,
        )
        assert abs(coord_restarted.calibration_offset_db - 104.0) <= 0.2
        assert abs(audio_restarted.calibration_offset_db - 104.0) <= 0.2
        assert coord_restarted.calibrated_at is not None
        store_restarted.close()
        audio_restarted.close()

    print("main self-test passed")


def verify_audio_source_ready(audio: NightAudioMonitor) -> None:
    """Exit with a clear message if live audio isn't implemented yet."""
    if not audio.use_fake:
        try:
            audio.read_window()
        except NotImplementedError as exc:
            print(f"[Error] {exc}", file=sys.stderr)
            sys.exit(1)


def make_uploader(upload_url: str) -> Optional[EventUploader]:
    if not upload_url:
        print("[Uploader] disabled (UPLOAD_URL empty)")
        return None
    print(f"[Uploader] sending events as JSON to {upload_url} as {DEVICE_ID!r} @ {DEVICE_LAT},{DEVICE_LON}")
    return EventUploader(upload_url, DEVICE_ID, DEVICE_LAT, DEVICE_LON)


def run_laptop_dev_server(
    port: int, db_path: str, source_mode: str, sim_buttons: bool, upload_url: str = UPLOAD_URL
) -> None:
    """Events mode on a laptop: serves the page, SSE at /api/events, /api/cmd and /export.csv."""
    from sim_imu_replay import SketchNoiseSimulator

    audio = NightAudioMonitor(
        use_fake=(source_mode == "fake"),
        calibration_offset_db=CALIBRATION_OFFSET_DB,
    )
    verify_audio_source_ready(audio)

    store = NoiseStore(db_path=db_path, retention_days=RETENTION_DAYS, grid_cell=GRID_CELL)
    ui = LaptopDevWebUI(log_events=False)
    bridge = BridgeSink(verbose=False)
    coord = NightNoiseCoordinator(
        audio=audio, store=store, ui=ui, bridge=bridge, uploader=make_uploader(upload_url)
    )
    mcu_sim = SketchNoiseSimulator(
        on_event_start=coord.on_mcu_event_start,
        on_event_end=coord.on_mcu_event_end,
        on_woke_me=coord.on_mcu_woke_me,
        on_tamper=coord.on_mcu_tamper,
        on_heartbeat=coord.on_mcu_heartbeat,
    )
    bridge.attach_laptop_simulator(mcu_sim)
    coord.sync_startup_thresholds(sim_now_ms=0)
    mcu_sim.tick(0)

    stop_event = threading.Event()

    def loop_worker() -> None:
        step_num = 0
        while not stop_event.is_set():
            step_num += 1
            sim_ms = step_num * int(WINDOW_INTERVAL_SEC * 1000)
            coord.step_once(sim_now_ms=sim_ms)
            if sim_buttons and step_num % 48 == 24:
                mcu_sim.press_woke_button(sim_ms)
            time.sleep(WINDOW_INTERVAL_SEC)

    threading.Thread(target=loop_worker, daemon=True).start()

    assets_dir = str(ROOT_DIR / "assets")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=assets_dir, **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/socket.io/socket.io.js"):
                stub = b"/* Laptop standalone mode: WebUI Socket.IO not active; using SSE */\n"
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(stub)))
                self.end_headers()
                self.wfile.write(stub)
                return

            if self.path.startswith("/api/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q: queue.Queue[str] = queue.Queue(maxsize=128)
                with ui._lock:
                    ui.sse_clients.append(q)
                coord.handle_request_state({})
                try:
                    while not stop_event.is_set():
                        msg = q.get(timeout=15.0)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                except Exception:
                    pass
                return

            if self.path.startswith("/export.csv"):
                csv_bytes = store.export_csv_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="night-noise-events.csv"',
                )
                self.send_header("Content-Length", str(len(csv_bytes)))
                self.end_headers()
                self.wfile.write(csv_bytes)
                return

            super().do_GET()

        def do_POST(self) -> None:
            if self.path.startswith("/api/cmd"):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw)
                ev = str(body.get("event", ""))
                payload = body.get("payload", {})
                if ev == "sim_woke_me":
                    mcu_sim.press_woke_button(bridge._sim_clock_ms + 10)
                elif ev == "sim_tamper":
                    coord.on_mcu_tamper()
                else:
                    ui.dispatch_from_client(ev, payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            self.send_error(404)

    os.makedirs(assets_dir, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"[Night-Noise Box] source={source_mode} profile={coord.active_profile} "
        f"serving assets/index.html at http://localhost:{port}"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        store.close()
        audio.close()


def run_uno_q_board(source_mode: str, db_path: str, upload_url: str = UPLOAD_URL) -> None:
    """Events mode on the board."""
    audio = NightAudioMonitor(
        use_fake=(source_mode == "fake"),
        calibration_offset_db=CALIBRATION_OFFSET_DB,
    )
    verify_audio_source_ready(audio)

    web_ui = WebUI()
    store = NoiseStore(db_path=db_path, retention_days=RETENTION_DAYS, grid_cell=GRID_CELL)
    bridge = BridgeSink(verbose=False)
    coord = NightNoiseCoordinator(
        audio=audio, store=store, ui=web_ui, bridge=bridge, uploader=make_uploader(upload_url)
    )

    Bridge.provide("event_start", coord.on_mcu_event_start)
    Bridge.provide("event_end", coord.on_mcu_event_end)
    Bridge.provide("woke_me", coord.on_mcu_woke_me)
    Bridge.provide("tamper", coord.on_mcu_tamper)
    Bridge.provide("heartbeat", coord.on_mcu_heartbeat)
    Bridge.provide("mcu_level", coord.on_mcu_level)
    Bridge.provide("knob", coord.on_mcu_knob)

    def audio_window_loop() -> None:
        time.sleep(0.5)
        coord.sync_startup_thresholds()
        coord.broadcast_heatmap()
        coord.broadcast_diagnostics()
        while True:
            t0 = time.monotonic()
            coord.step_once()
            elapsed = time.monotonic() - t0
            time.sleep(max(0.005, WINDOW_INTERVAL_SEC - elapsed))

    threading.Thread(target=audio_window_loop, daemon=True).start()
    App.run()


def run_analysis_mode(db_path: str, readings_url: str, mic_kind: Optional[str] = None) -> None:
    """Analysis mode. On the board the STM32 still gets levels for its LED bar."""
    from analysis import build_analyzer, run_forever
    from config import MIC_SOURCE

    saved = load_saved_calibration(default_calibration_path_for_db(db_path))
    offset = saved[0] if saved is not None else CALIBRATION_OFFSET_DB
    on_level = None
    if ON_UNO_Q and Bridge is not None:
        # The sketch still sends these; nothing uses them in this mode.
        for name in ("event_start", "event_end", "woke_me", "tamper", "heartbeat", "mcu_level", "knob"):
            Bridge.provide(name, lambda *_a: None)
        on_level = lambda db: Bridge.notify("level", db)  # noqa: E731

    analyzer = build_analyzer(mic_kind or MIC_SOURCE, calibration_offset_db=offset, on_level=on_level)
    if ON_UNO_Q and App is not None:
        threading.Thread(target=run_forever, args=(analyzer, readings_url), daemon=True).start()
        App.run()
    else:
        run_forever(analyzer, readings_url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Night-Noise Box")
    parser.add_argument(
        "--source",
        choices=("fake", "live"),
        default=None,
        help=f"Audio source mode overriding config.py (default from config.py: {AUDIO_SOURCE!r})",
    )
    parser.add_argument("--self-test", action="store_true", help="Run the events-mode self-test")
    parser.add_argument("--laptop-server", action="store_true", help="Force laptop HTTP/SSE server mode")
    parser.add_argument("--port", type=int, default=7000, help="Laptop HTTP/SSE port (default 7000)")
    parser.add_argument("--db", type=str, default=str(THIS_DIR / "night_noise.sqlite3"), help="SQLite DB path")
    parser.add_argument("--sim-buttons", action="store_true", help="Periodically simulate woke_me presses on laptop")
    parser.add_argument(
        "--upload-url",
        type=str,
        default=UPLOAD_URL,
        help=f"Noise Map server endpoint for JSON event uploads ('' disables; default {UPLOAD_URL!r})",
    )
    parser.add_argument("--fake-audio", action="store_true", help="Alias for --source fake")
    parser.add_argument(
        "--mode",
        choices=("analysis", "events"),
        default=RUN_MODE,
        help=f"analysis = periodic mic analysis + upload; events = MCU event pipeline (default {RUN_MODE!r})",
    )
    parser.add_argument("--mic", choices=("alsa", "synthetic"), default=None, help="Analysis mode mic (default MIC_SOURCE)")
    parser.add_argument("--readings-url", type=str, default=READINGS_URL, help="Analysis mode upload endpoint ('' = print only)")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    if args.mode == "analysis":
        run_analysis_mode(args.db, args.readings_url, args.mic)
        return

    resolved_source = args.source if args.source is not None else AUDIO_SOURCE
    if args.fake_audio:
        resolved_source = "fake"
    if resolved_source not in ("fake", "live"):
        print(f"[Error] Invalid AUDIO_SOURCE {resolved_source!r}; expected 'fake' or 'live'.", file=sys.stderr)
        sys.exit(1)

    if ON_UNO_Q and not args.laptop_server:
        run_uno_q_board(source_mode=resolved_source, db_path=args.db, upload_url=args.upload_url)
    else:
        run_laptop_dev_server(
            port=args.port,
            db_path=args.db,
            source_mode=resolved_source,
            sim_buttons=args.sim_buttons,
            upload_url=args.upload_url,
        )


if __name__ == "__main__":
    main()
