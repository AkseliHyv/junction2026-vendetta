#!/usr/bin/env python3
"""Python copy of the event logic in sketch/sketch.ino, for testing without a board.

main.py uses it in place of the STM32 when running on a laptop. Keep the
constants and logic in sync with the sketch.

    python3 python/sim_imu_replay.py    # self-test
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Tuple

# Same values as the top of sketch.ino
START_THRESHOLD_DB: float = 52.0
END_THRESHOLD_DB: float = 45.0
START_HOLD_MS: int = 375
END_HOLD_MS: int = 625
NO_SIGNAL_TIMEOUT_MS: int = 2000
HEARTBEAT_INTERVAL_MS: int = 1000

LEVEL_BAR_MIN_DB: float = 30.0
LEVEL_BAR_MAX_DB: float = 78.0
NUM_PIXELS: int = 8

IMU_INTERVAL_MS: int = 50
WOKE_DEBOUNCE_MS: int = 200
TAMPER_DEBOUNCE_MS: int = 1000
GRAVITY_EMA_ALPHA: float = 0.15
TAMPER_THRESHOLD_G: float = 0.28


def clampf(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clampi(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def level_db_to_lit_leds(db: float) -> int:
    if db <= LEVEL_BAR_MIN_DB:
        return 0
    ratio = (db - LEVEL_BAR_MIN_DB) / (LEVEL_BAR_MAX_DB - LEVEL_BAR_MIN_DB)
    lit = int(clampf(ratio, 0.0, 1.0) * NUM_PIXELS + 0.999)
    return clampi(lit, 0, NUM_PIXELS)


class SketchNoiseSimulator:
    """Mirrors the sketch's state. Callbacks stand in for Bridge.notify."""

    def __init__(
        self,
        on_event_start: Optional[Callable[[], None]] = None,
        on_event_end: Optional[Callable[[int], None]] = None,
        on_woke_me: Optional[Callable[[], None]] = None,
        on_tamper: Optional[Callable[[], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
    ) -> None:
        self._cb_start = on_event_start
        self._cb_end = on_event_end
        self._cb_woke = on_woke_me
        self._cb_tamper = on_tamper
        self._cb_heartbeat = on_heartbeat

        # Changed by rx_set_thresholds
        self.active_start_db: float = START_THRESHOLD_DB
        self.active_end_db: float = END_THRESHOLD_DB
        self.active_start_hold_ms: int = START_HOLD_MS
        self.active_end_hold_ms: int = END_HOLD_MS

        self.current_level_db: float = 0.0
        self.current_label: str = "background"
        self.current_confidence: float = 0.0
        self.has_signal: bool = False
        self.last_level_rx_ms: int = 0
        self.last_heartbeat_ms: Optional[int] = None

        self.event_open: bool = False
        self.event_start_ms: int = 0
        self.above_start_armed: bool = False
        self.above_start_since_ms: int = 0
        self.below_end_armed: bool = False
        self.below_end_since_ms: int = 0

        self.gravity_ema: float = 1.0
        self.imu_initialized: bool = False
        self.last_woke_ms: Optional[int] = None
        self.last_tamper_ms: Optional[int] = None

        self.pixels_mode: str = "no_signal"  # "no_signal" | "bar_0".."bar_8"
        self.events: List[Tuple[int, str, Optional[int]]] = []
        self.heartbeat_count: int = 0

    def _emit_start(self, now_ms: int) -> None:
        self.events.append((now_ms, "event_start", None))
        if self._cb_start is not None:
            self._cb_start()

    def _emit_end(self, now_ms: int, duration_ms: int) -> None:
        self.events.append((now_ms, "event_end", duration_ms))
        if self._cb_end is not None:
            self._cb_end(duration_ms)

    def _emit_heartbeat(self, now_ms: int) -> None:
        self.heartbeat_count += 1
        self.events.append((now_ms, "heartbeat", None))
        if self._cb_heartbeat is not None:
            self._cb_heartbeat()

    def _refresh_pixels(self) -> None:
        if not self.has_signal:
            self.pixels_mode = "no_signal"
        else:
            self.pixels_mode = f"bar_{level_db_to_lit_leds(self.current_level_db)}"

    # --- Messages from Linux ---
    def rx_level(self, now_ms: int, db: float) -> None:
        self.current_level_db = clampf(float(db), 0.0, 120.0)
        self.has_signal = True
        self.last_level_rx_ms = now_ms
        self.tick(now_ms)

    def rx_label(self, _now_ms: int, name: str, confidence: float) -> None:
        self.current_label = str(name)
        self.current_confidence = clampf(float(confidence), 0.0, 1.0)

    def rx_set_thresholds(
        self,
        _now_ms: int,
        start_db: float,
        end_db: float,
        start_hold_ms: int,
        end_hold_ms: int,
    ) -> None:
        safe_start = clampf(float(start_db), 5.0, 120.0)
        safe_end = clampf(float(end_db), 0.0, safe_start - 1.0)
        self.active_start_db = safe_start
        self.active_end_db = safe_end
        self.active_start_hold_ms = max(0, int(start_hold_ms))
        self.active_end_hold_ms = max(0, int(end_hold_ms))
        self.above_start_armed = False
        self.below_end_armed = False

    # --- Main loop ---
    def tick(self, now_ms: int) -> None:
        if (
            self.last_heartbeat_ms is None
            or (now_ms - self.last_heartbeat_ms) >= HEARTBEAT_INTERVAL_MS
        ):
            self.last_heartbeat_ms = now_ms
            self._emit_heartbeat(now_ms)

        if self.has_signal and (now_ms - self.last_level_rx_ms) >= NO_SIGNAL_TIMEOUT_MS:
            self.has_signal = False
            self.above_start_armed = False
            self.below_end_armed = False
            if self.event_open:
                dur = max(0, now_ms - self.event_start_ms)
                self.event_open = False
                self._emit_end(now_ms, dur)
            self._refresh_pixels()
            return

        if not self.has_signal:
            self._refresh_pixels()
            return

        if not self.event_open:
            if self.current_level_db >= self.active_start_db:
                if not self.above_start_armed:
                    self.above_start_armed = True
                    self.above_start_since_ms = now_ms
                elif (now_ms - self.above_start_since_ms) >= self.active_start_hold_ms:
                    self.event_open = True
                    self.event_start_ms = now_ms
                    self.above_start_armed = False
                    self.below_end_armed = False
                    self._emit_start(now_ms)
            else:
                self.above_start_armed = False
        else:
            if self.current_level_db <= self.active_end_db:
                if not self.below_end_armed:
                    self.below_end_armed = True
                    self.below_end_since_ms = now_ms
                elif (now_ms - self.below_end_since_ms) >= self.active_end_hold_ms:
                    dur = max(0, now_ms - self.event_start_ms)
                    self.event_open = False
                    self.below_end_armed = False
                    self._emit_end(now_ms, dur)
            else:
                self.below_end_armed = False

        self._refresh_pixels()

    # --- Button and IMU ---
    def press_woke_button(self, now_ms: int) -> None:
        if self.last_woke_ms is None or (now_ms - self.last_woke_ms) >= WOKE_DEBOUNCE_MS:
            self.last_woke_ms = now_ms
            self.events.append((now_ms, "woke_me", None))
            if self._cb_woke is not None:
                self._cb_woke()

    def step_imu(self, now_ms: int, ax: float, ay: float, az: float) -> None:
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        if not self.imu_initialized:
            self.gravity_ema = mag
            self.imu_initialized = True
            return
        self.gravity_ema = (1.0 - GRAVITY_EMA_ALPHA) * self.gravity_ema + GRAVITY_EMA_ALPHA * mag
        dyn = abs(mag - self.gravity_ema)
        if dyn >= TAMPER_THRESHOLD_G and (
            self.last_tamper_ms is None or (now_ms - self.last_tamper_ms) >= TAMPER_DEBOUNCE_MS
        ):
            self.last_tamper_ms = now_ms
            self.events.append((now_ms, "tamper", None))
            if self._cb_tamper is not None:
                self._cb_tamper()


def run_self_test() -> None:
    sim = SketchNoiseSimulator()
    assert sim.pixels_mode == "no_signal"

    # Night profile (52 / 45 dB)
    trace = [
        (0, 35.0),
        (125, 36.0),
        (250, 55.0),   # above start threshold from here
        (375, 58.0),
        (500, 62.0),
        (625, 64.0),   # held for 375 ms: event_start
        (750, 48.5),   # between the thresholds, so the event stays open
        (875, 56.0),
        (1000, 42.0),  # below end threshold from here
        (1125, 40.0),
        (1250, 38.0),
        (1375, 36.0),
        (1500, 35.0),
        (1625, 35.0),  # held for 625 ms: event_end(1000)
    ]
    for t_ms, db in trace:
        sim.rx_level(t_ms, db)
        if t_ms == 750:
            sim.press_woke_button(750)
            sim.press_woke_button(830)  # within the debounce time, ignored

    assert ("event_start", None) in [(e[1], e[2]) for e in sim.events], "Expected event_start"
    ends = [e for e in sim.events if e[1] == "event_end"]
    assert len(ends) == 1 and ends[0][2] == 1000, f"Expected 1 event_end(1000), got {ends}"
    wokes = [e for e in sim.events if e[1] == "woke_me"]
    assert len(wokes) == 1, f"Expected 1 debounced woke_me, got {wokes}"
    assert sim.heartbeat_count >= 2, f"Expected >=2 heartbeats (t=0, t=1000), got {sim.heartbeat_count}"

    # Tilting the box triggers one tamper.
    sim.step_imu(1700, 0.0, 0.0, 1.0)
    sim.step_imu(1750, 0.0, 0.0, 1.0)
    sim.step_imu(1800, 0.45, 0.0, 1.35)
    sim.step_imu(1900, 0.45, 0.0, 1.35)
    tampers = [e for e in sim.events if e[1] == "tamper"]
    assert len(tampers) == 1, f"Expected 1 debounced tamper, got {tampers}"

    # 2 s without a level closes the open event and shows "no signal".
    sim.rx_level(2000, 60.0)
    sim.rx_level(2200, 61.0)
    sim.rx_level(2400, 62.0)
    assert sim.event_open is True and sim.pixels_mode.startswith("bar_")
    sim.tick(4450)
    assert sim.event_open is False
    assert sim.pixels_mode == "no_signal"
    ends_after_wd = [e for e in sim.events if e[1] == "event_end"]
    assert len(ends_after_wd) == 2 and ends_after_wd[1][2] == 2050

    # Demo profile (70 / 62 dB): 62 dB no longer starts an event, 74 dB does.
    sim.rx_set_thresholds(5000, 70.0, 62.0, 375, 625)
    starts_before_demo = len([e for e in sim.events if e[1] == "event_start"])
    for idx in range(6):
        sim.rx_level(5000 + idx * 125, 62.0)
    assert len([e for e in sim.events if e[1] == "event_start"]) == starts_before_demo
    assert sim.event_open is False

    for idx in range(5):
        sim.rx_level(6000 + idx * 125, 74.0)
    assert sim.event_open is True
    assert len([e for e in sim.events if e[1] == "event_start"]) == starts_before_demo + 1
    for idx in range(6):
        sim.rx_level(6625 + idx * 125, 58.0)
    assert sim.event_open is False

    for t_ms, name, arg in sim.events:
        if name == "heartbeat":
            continue
        arg_str = "" if arg is None else str(arg)
        print(f"[{t_ms:5d} ms] Bridge TX -> {name}({arg_str})")
    print("sketch simulator self-test passed")


if __name__ == "__main__":
    run_self_test()
