#!/usr/bin/env python3
"""
Workstream 4b: Glue, Mode State Machine & Instruct Engine (python/main.py)
--------------------------------------------------------------------------
Connects:
  1. Microcontroller (STM32) via `arduino.app_utils.Bridge`
  2. Seated Arm Pose Estimator (`python/pose.py`)
  3. Browser Web UI (`assets/index.html`) via `arduino.app_bricks.web_ui.WebUI`

Owns authoritative mode (`free` | `instruct`), tolerance (from Modulino Knob),
preset & custom exercise sessions, hold-time matching, wrist-only fallback when
camera/person is absent, and strike-to-sound latency logging.

Board runtime (Arduino App Lab):
    python3 python/main.py

Laptop standalone checks (no UNO Q required):
    python3 python/main.py --self-test
    python3 python/main.py --laptop-server --fake-pose --sim-strikes
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure sibling imports work whether invoked from repo root or python/
THIS_DIR = Path(__file__).resolve().parent
ROOT_DIR = THIS_DIR.parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from exercises import EXERCISES_BY_ID, PRESET_EXERCISES, ExerciseStep  # noqa: E402
from pose import PoseReading, SeatedArmPoseEstimator  # noqa: E402

# ---------------------------------------------------------------------------
# Constants from CONTRACT.md
# ---------------------------------------------------------------------------
PENTATONIC_MIDI = [60, 62, 64, 67, 69, 72, 74, 76]  # C4 D4 E4 G4 A4 C5 D5 E5
FALLBACK_ARPEGGIO = [60, 64, 67, 69, 72, 69, 67, 64]
REWARD_PHRASE_MIDI = [67, 72, 76]                   # G4 -> C5 -> E5 gentle uplift
SESSION_MELODY_MIDI = [60, 64, 67, 72, 76, 72, 76]  # Warm celebratory cadence

POSE_STALE_SEC = 1.0
STATE_TICK_SEC = 1.0 / 15.0  # 15 Hz


def now_ms() -> int:
    return int(time.time() * 1000)


def arm_height_to_midi(height: float) -> int:
    clamped = max(0.0, min(1.0, float(height)))
    idx = min(7, int(clamped * 8.0))
    return PENTATONIC_MIDI[idx]


def knob_to_tolerance(knob_value: int) -> float:
    """Maps Knob 0..100 -> 0.06 (strict) .. 0.28 (gentle). Default 50 -> 0.17."""
    k = max(0, min(100, int(knob_value)))
    return round(0.06 + (k / 100.0) * 0.22, 3)


# ---------------------------------------------------------------------------
# Hardware / Laptop Transport Adapters
# ---------------------------------------------------------------------------
try:
    from arduino.app_utils import App, Bridge  # type: ignore
    from arduino.app_bricks.web_ui import WebUI  # type: ignore

    ON_UNO_Q = True
except ImportError:
    ON_UNO_Q = False
    App = None  # type: ignore
    Bridge = None  # type: ignore
    WebUI = None  # type: ignore


class LaptopDevWebUI:
    """
    Used ONLY when `--laptop-server` or `--self-test` is run on a laptop without
    `arduino.app_bricks.web_ui`. On the UNO Q board, the official `WebUI` brick
    is always used directly with zero extra servers.
    """

    def __init__(self, log_events: bool = True) -> None:
        self.handlers: Dict[str, Callable[..., None]] = {}
        self.sent_log: List[tuple[str, Dict[str, Any]]] = []
        self.sse_clients: List[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self.log_events = log_events

    def send_message(self, event: str, payload: Dict[str, Any]) -> None:
        if self.log_events and event != "state":
            print(f"[WebUI TX] {event}: {json.dumps(payload)}")
        with self._lock:
            self.sent_log.append((event, payload))
            if len(self.sent_log) > 200:
                self.sent_log = self.sent_log[-200:]
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
    """Wraps `arduino.app_utils.Bridge` or logs MCU commands during laptop runs."""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.last_pixels = "idle"

    def notify_pixels(self, pattern: str) -> None:
        if pattern == self.last_pixels and pattern != "free_pulse":
            return
        self.last_pixels = pattern
        if ON_UNO_Q and Bridge is not None:
            Bridge.notify("set_pixels", pattern)
        elif self.verbose:
            print(f"[Bridge TX -> MCU] set_pixels({pattern!r})")


# ---------------------------------------------------------------------------
# Core Instrument & Exercise Coordinator
# ---------------------------------------------------------------------------
class MovementToMusicCoordinator:
    def __init__(
        self,
        pose_estimator: SeatedArmPoseEstimator,
        ui: Any,
        bridge_sink: BridgeSink,
    ) -> None:
        self.pose = pose_estimator
        self.ui = ui
        self.bridge = bridge_sink
        self._lock = threading.RLock()

        # Sensor state
        self.left_level: float = 0.20
        self.right_level: float = 0.20
        self.person_present: bool = False
        self.pose_fps: float = 0.0
        self.last_valid_pose_mono: float = 0.0
        self.bpm: int = 92
        self.tolerance: float = knob_to_tolerance(50)
        self.arpeggio_idx: int = 0

        # Mode & exercise session state
        self.mode: str = "free"  # "free" | "instruct"
        self.exercise_id: str = PRESET_EXERCISES[0]["id"]
        self.custom_steps: List[ExerciseStep] = []

        self.step_index: int = 0
        self.session_active: bool = False
        self.session_start_mono: float = 0.0
        self.hold_accum_sec: float = 0.0
        self.last_tick_mono: float = time.monotonic()
        self.notes_played_in_session: int = 0
        self.pixels_pattern: str = "idle"

        self._register_ui_handlers()

    def _wrap_ui_cb(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[..., None]:
        """
        Arduino App Lab's `WebUI.on_message` invokes callbacks as `(sid, data)`
        via python-socketio. Accepting `*args` safely handles both `(sid, data)`
        and single-arg `(data,)` test harnesses.
        """

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
        self.ui.on_message("set_mode", self._wrap_ui_cb(self.handle_set_mode))
        self.ui.on_message("start_session", self._wrap_ui_cb(self.handle_start_session))
        self.ui.on_message("record_step", self._wrap_ui_cb(self.handle_record_step))
        self.ui.on_message("clear_custom", self._wrap_ui_cb(self.handle_clear_custom))
        self.ui.on_message("client_latency", self._wrap_ui_cb(self.handle_client_latency))

    # --- Helpers ---
    def _catalog_summary(self) -> List[Dict[str, Any]]:
        items = [
            {"id": ex["id"], "name": ex["name"], "step_count": len(ex["steps"])}
            for ex in PRESET_EXERCISES
        ]
        items.append(
            {
                "id": "custom",
                "name": "Custom Physio Routine",
                "step_count": len(self.custom_steps),
            }
        )
        return items

    def _active_steps_and_name(self) -> tuple[List[ExerciseStep], str]:
        if self.exercise_id == "custom":
            if self.custom_steps:
                return self.custom_steps, "Custom Physio Routine"
            return PRESET_EXERCISES[0]["steps"], PRESET_EXERCISES[0]["name"]
        ex = EXERCISES_BY_ID.get(self.exercise_id, PRESET_EXERCISES[0])
        return ex["steps"], ex["name"]

    def _is_fallback_wrist_only(self) -> bool:
        if not self.person_present:
            return True
        if (time.monotonic() - self.last_valid_pose_mono) > POSE_STALE_SEC:
            return True
        return False

    def _set_pixels(self, pattern: str) -> None:
        self.pixels_pattern = pattern
        self.bridge.notify_pixels(pattern)

    # --- Outbound Broadcasts (CONTRACT.md Channel B) ---
    def broadcast_mode(self) -> None:
        with self._lock:
            self.ui.send_message(
                "mode",
                {
                    "mode": self.mode,
                    "exercise_id": self.exercise_id,
                    "exercises": self._catalog_summary(),
                    "custom_count": len(self.custom_steps),
                    "ts_ms": now_ms(),
                },
            )

    def broadcast_current_step(self) -> None:
        with self._lock:
            steps, ex_name = self._active_steps_and_name()
            if not steps or self.step_index >= len(steps):
                return
            st = steps[self.step_index]
            self.ui.send_message(
                "step",
                {
                    "index": self.step_index,
                    "total": len(steps),
                    "left": st["left"],
                    "right": st["right"],
                    "tolerance": self.tolerance,
                    "hold_sec": st["hold_sec"],
                    "instruction": st["instruction"],
                    "exercise_name": ex_name,
                    "ts_ms": now_ms(),
                },
            )

    # --- MCU Bridge Handlers (CONTRACT.md Channel A) ---
    def on_mcu_strike(self, intensity: float) -> None:
        strike_received_ms = now_ms()
        vel = round(max(0.05, min(1.0, float(intensity))), 3)

        with self._lock:
            fallback = self._is_fallback_wrist_only()
            if fallback:
                note_l = FALLBACK_ARPEGGIO[self.arpeggio_idx % len(FALLBACK_ARPEGGIO)]
                note_r = PENTATONIC_MIDI[(PENTATONIC_MIDI.index(note_l) + 2) % len(PENTATONIC_MIDI)]
                self.arpeggio_idx += 1
            else:
                note_l = arm_height_to_midi(self.left_level)
                note_r = arm_height_to_midi(self.right_level)

            # Primary left-arm marimba voice
            self.ui.send_message(
                "note_on",
                {
                    "note": note_l,
                    "velocity": vel,
                    "voice": 0,
                    "ts_ms": strike_received_ms,
                },
            )
            self.notes_played_in_session += 1

            # Second right-arm harmonic bell voice when strike is medium/strong or arms differ
            if vel >= 0.25 or note_r != note_l:
                self.ui.send_message(
                    "note_on",
                    {
                        "note": note_r,
                        "velocity": round(vel * 0.82, 3),
                        "voice": 1,
                        "ts_ms": strike_received_ms,
                    },
                )
                self.notes_played_in_session += 1

            if self.mode == "free":
                self._set_pixels("free_pulse")

    def on_mcu_tempo(self, bpm: int) -> None:
        with self._lock:
            self.bpm = max(30, min(220, int(bpm)))

    def on_mcu_knob(self, value: int) -> None:
        with self._lock:
            new_tol = knob_to_tolerance(int(value))
            if abs(new_tol - self.tolerance) >= 0.002:
                self.tolerance = new_tol
                if self.mode == "instruct" and self.session_active:
                    self.broadcast_current_step()

    def on_mcu_button(self, button_id: int) -> None:
        btn = int(button_id)
        with self._lock:
            if btn == 0:
                # Button A: Toggle Free <-> Instruct mode
                next_mode = "instruct" if self.mode == "free" else "free"
                self.handle_set_mode({"mode": next_mode})
            elif btn == 1:
                # Button B: Cycle to next preset exercise & start session
                ids = [ex["id"] for ex in PRESET_EXERCISES]
                if self.custom_steps:
                    ids.append("custom")
                cur_idx = ids.index(self.exercise_id) if self.exercise_id in ids else 0
                next_id = ids[(cur_idx + 1) % len(ids)]
                self.handle_start_session({"exercise_id": next_id})
            elif btn == 2:
                # Button C: Physio records current pose as custom step
                self.handle_record_step({"hold_sec": 1.5})

    # --- Browser Event Handlers (CONTRACT.md Channel C) ---
    def handle_set_mode(self, payload: Dict[str, Any]) -> None:
        requested = str(payload.get("mode", "free"))
        if requested not in ("free", "instruct"):
            return
        with self._lock:
            self.mode = requested
            if self.mode == "instruct":
                self._begin_session_locked(self.exercise_id)
            else:
                self.session_active = False
                self.hold_accum_sec = 0.0
                self._set_pixels("idle")
            self.broadcast_mode()
            if self.mode == "instruct":
                self.broadcast_current_step()

    def handle_start_session(self, payload: Dict[str, Any]) -> None:
        ex_id = str(payload.get("exercise_id", self.exercise_id))
        with self._lock:
            if ex_id == "custom" and not self.custom_steps:
                ex_id = PRESET_EXERCISES[0]["id"]
            elif ex_id != "custom" and ex_id not in EXERCISES_BY_ID:
                ex_id = PRESET_EXERCISES[0]["id"]
            self.mode = "instruct"
            self._begin_session_locked(ex_id)
            self.broadcast_mode()
            self.broadcast_current_step()

    def _begin_session_locked(self, ex_id: str) -> None:
        self.exercise_id = ex_id
        self.step_index = 0
        self.hold_accum_sec = 0.0
        self.notes_played_in_session = 0
        self.session_start_mono = time.monotonic()
        self.session_active = True
        self._set_pixels("progress_0")

    def handle_record_step(self, payload: Dict[str, Any]) -> None:
        hold_sec = float(payload.get("hold_sec", 1.5))
        hold_sec = max(0.5, min(5.0, hold_sec))
        with self._lock:
            idx = len(self.custom_steps) + 1
            step: ExerciseStep = {
                "left": round(self.left_level, 2),
                "right": round(self.right_level, 2),
                "tolerance": self.tolerance,
                "hold_sec": round(hold_sec, 1),
                "instruction": f"Hold custom pose {idx} gently and breathe",
            }
            self.custom_steps.append(step)
            print(f"[Physio] Recorded custom step #{idx}: left={step['left']}, right={step['right']}")
            self.broadcast_mode()

    def handle_clear_custom(self, _payload: Dict[str, Any]) -> None:
        with self._lock:
            self.custom_steps.clear()
            if self.exercise_id == "custom":
                self.exercise_id = PRESET_EXERCISES[0]["id"]
            self.broadcast_mode()

    def handle_client_latency(self, payload: Dict[str, Any]) -> None:
        lat = float(payload.get("latency_ms", 0.0))
        print(f"[Latency] Strike-to-sound browser latency: {lat:.1f} ms (target < 100 ms)")

    # --- Periodic 15 Hz Pose & Hold Loop ---
    def step_once(self, injected_reading: Optional[PoseReading] = None) -> None:
        reading = injected_reading if injected_reading is not None else self.pose.read()
        now_mono = time.monotonic()

        with self._lock:
            dt = max(0.0, min(0.25, now_mono - self.last_tick_mono))
            self.last_tick_mono = now_mono

            self.left_level = reading.left
            self.right_level = reading.right
            self.person_present = reading.person_present
            self.pose_fps = reading.fps
            if reading.person_present:
                self.last_valid_pose_mono = now_mono

            hold_ratio = 0.0
            if self.mode == "instruct" and self.session_active:
                steps, ex_name = self._active_steps_and_name()
                if steps and self.step_index < len(steps):
                    target = steps[self.step_index]
                    tol = self.tolerance
                    matched = (
                        abs(self.left_level - target["left"]) <= tol
                        and abs(self.right_level - target["right"]) <= tol
                    )
                    if matched:
                        self.hold_accum_sec += dt
                    else:
                        # Gentle decay rather than harsh reset if an older adult wobbles slightly
                        self.hold_accum_sec = max(0.0, self.hold_accum_sec - dt * 0.5)

                    hold_ratio = min(1.0, self.hold_accum_sec / max(0.2, target["hold_sec"]))

                    if self.hold_accum_sec >= target["hold_sec"]:
                        reached_idx = self.step_index
                        total = len(steps)
                        self.ui.send_message(
                            "step_reached",
                            {"index": reached_idx, "total": total, "ts_ms": now_ms()},
                        )
                        self.step_index += 1
                        self.hold_accum_sec = 0.0
                        hold_ratio = 0.0

                        # Map progress across the 8 Modulino Pixels
                        lit = round((self.step_index / total) * 8)
                        self._set_pixels(f"progress_{lit}")

                        if self.step_index >= total:
                            self.session_active = False
                            duration = round(now_mono - self.session_start_mono, 1)
                            self._set_pixels("celebrate")
                            self.ui.send_message(
                                "target_melody",
                                {
                                    "kind": "session_complete",
                                    "notes": SESSION_MELODY_MIDI,
                                    "ts_ms": now_ms(),
                                },
                            )
                            self.ui.send_message(
                                "session_done",
                                {
                                    "steps_completed": total,
                                    "total_steps": total,
                                    "duration_sec": duration,
                                    "exercise_name": ex_name,
                                    "notes_played": self.notes_played_in_session,
                                    "ts_ms": now_ms(),
                                },
                            )
                        else:
                            self.ui.send_message(
                                "target_melody",
                                {
                                    "kind": "step_reward",
                                    "notes": REWARD_PHRASE_MIDI,
                                    "ts_ms": now_ms(),
                                },
                            )
                            self.broadcast_current_step()

            self.ui.send_message(
                "state",
                {
                    "left": self.left_level,
                    "right": self.right_level,
                    "person_present": self.person_present,
                    "pose_fps": self.pose_fps,
                    "fallback_wrist_only": self._is_fallback_wrist_only(),
                    "tolerance": self.tolerance,
                    "bpm": self.bpm,
                    "hold_progress": round(hold_ratio, 3),
                    "pixels": self.pixels_pattern,
                    "ts_ms": now_ms(),
                },
            )


# ---------------------------------------------------------------------------
# Laptop End-to-End Self-Test & Optional Local Dev Server
# ---------------------------------------------------------------------------
def run_self_test() -> None:
    print("=== Workstream 4b End-to-End Check: python/main.py + exercises.py + pose.py ===")
    estimator = SeatedArmPoseEstimator(use_fake=True, smoothing_alpha=1.0)
    ui = LaptopDevWebUI(log_events=True)
    bridge = BridgeSink(verbose=True)
    coord = MovementToMusicCoordinator(estimator, ui, bridge)

    # 1. Verify camera-absent wrist-only fallback in Free mode
    coord.step_once(PoseReading(left=0.2, right=0.2, person_present=False, fps=0.0, source="camera_unavailable"))
    t0 = now_ms()
    coord.on_mcu_strike(0.75)
    t1 = now_ms()
    coord.handle_client_latency({"latency_ms": float(max(1, t1 - t0) + 6.2), "strike_ts_ms": t0})
    assert any(ev == "note_on" for ev, _ in ui.sent_log), "Expected note_on during camera-absent fallback"

    # 2. Verify Physio custom step recording
    coord.step_once(estimator.set_simulated_levels(0.64, 0.32, present=True))
    coord.handle_record_step({"hold_sec": 0.6})
    assert len(coord.custom_steps) == 1, "Expected 1 custom step recorded"

    # 3. Complete a full `morning_sun` guided exercise session with simulated arm levels
    coord.handle_start_session({"exercise_id": "morning_sun"})
    steps = EXERCISES_BY_ID["morning_sun"]["steps"]
    for idx, st in enumerate(steps):
        print(f"--- Simulating patient holding step {idx + 1}/{len(steps)}: {st['instruction']} ---")
        ticks_needed = int((st["hold_sec"] / 0.1) + 2)
        for k in range(ticks_needed):
            coord.last_tick_mono = time.monotonic() - 0.1
            coord.step_once(estimator.set_simulated_levels(st["left"], st["right"], present=True))
            if k == 1:
                coord.on_mcu_strike(0.6)

    done_events = [p for ev, p in ui.sent_log if ev == "session_done"]
    assert len(done_events) == 1, f"Expected session_done after completing steps, got {len(done_events)}"
    summary = done_events[0]
    print(f"=== PASS: Full session completed ({summary['steps_completed']}/{summary['total_steps']} steps, "
          f"{summary['notes_played']} notes played, pixels={coord.pixels_pattern}) ===")
    estimator.close()


def run_laptop_dev_server(port: int, use_fake_pose: bool, sim_strikes: bool, cam_index: int) -> None:
    """Runs `assets/` + SSE live bridge on a developer laptop without UNO Q App Lab."""
    estimator = SeatedArmPoseEstimator(camera_source=cam_index, use_fake=use_fake_pose)
    ui = LaptopDevWebUI(log_events=False)
    bridge = BridgeSink(verbose=True)
    coord = MovementToMusicCoordinator(estimator, ui, bridge)

    stop_event = threading.Event()

    def loop_worker() -> None:
        last_strike = time.monotonic()
        while not stop_event.is_set():
            coord.step_once()
            if sim_strikes and (time.monotonic() - last_strike) >= 0.65:
                last_strike = time.monotonic()
                coord.on_mcu_strike(0.68)
            time.sleep(STATE_TICK_SEC)

    worker = threading.Thread(target=loop_worker, daemon=True)
    worker.start()

    assets_dir = str(ROOT_DIR / "assets")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=assets_dir, **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/api/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q: queue.Queue[str] = queue.Queue(maxsize=64)
                with ui._lock:
                    ui.sse_clients.append(q)
                coord.broadcast_mode()
                if coord.mode == "instruct":
                    coord.broadcast_current_step()
                try:
                    while not stop_event.is_set():
                        msg = q.get(timeout=15.0)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                except Exception:
                    pass
                return
            super().do_GET()

        def do_POST(self) -> None:
            if self.path.startswith("/api/cmd"):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw)
                ui.dispatch_from_client(str(body.get("event", "")), body.get("payload", {}))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            self.send_error(404)

    os.makedirs(assets_dir, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[Laptop Mode] Serving assets/index.html at http://localhost:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        estimator.close()


def run_uno_q_board() -> None:
    """Production entry point on Arduino UNO Q inside Arduino App Lab."""
    web_ui = WebUI()
    estimator = SeatedArmPoseEstimator(camera_source=0, use_fake=False)
    bridge = BridgeSink(verbose=False)
    coord = MovementToMusicCoordinator(estimator, web_ui, bridge)

    # Register MCU -> Linux RPC notifications (CONTRACT.md Channel A1)
    Bridge.provide("strike", coord.on_mcu_strike)
    Bridge.provide("tempo", coord.on_mcu_tempo)
    Bridge.provide("button", coord.on_mcu_button)
    Bridge.provide("knob", coord.on_mcu_knob)

    def pose_and_state_loop() -> None:
        time.sleep(0.5)
        coord.broadcast_mode()
        while True:
            coord.step_once()
            time.sleep(STATE_TICK_SEC)

    threading.Thread(target=pose_and_state_loop, daemon=True).start()
    App.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="UNO Q Movement-to-Music Linux Coordinator")
    parser.add_argument("--self-test", action="store_true", help="Run automated end-to-end session & latency check")
    parser.add_argument("--laptop-server", action="store_true", help="Serve assets/index.html on laptop without UNO Q")
    parser.add_argument("--port", type=int, default=7000, help="Laptop server port (default 7000)")
    parser.add_argument("--fake-pose", action="store_true", help="Use synthetic seated arm pose stream")
    parser.add_argument("--sim-strikes", action="store_true", help="Emit periodic simulated wrist strikes")
    parser.add_argument("--webcam", type=int, default=0, help="USB camera device index")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
    elif args.laptop_server or not ON_UNO_Q:
        if not args.laptop_server:
            print("[Info] `arduino.app_utils` not detected; running end-to-end self-test (pass --laptop-server to host UI).")
            run_self_test()
        else:
            run_laptop_dev_server(args.port, args.fake_pose, args.sim_strikes, args.webcam)
    else:
        run_uno_q_board()


if __name__ == "__main__":
    main()
