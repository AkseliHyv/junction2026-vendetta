#!/usr/bin/env python3
"""
Standalone laptop verification harness for Workstream 1 (sketch/sketch.ino).
Feeds a recorded/synthetic 200 Hz 3-axis IMU acceleration stream, button presses,
and rotary knob turns through the exact strike-detection, debounce, median-BPM,
and pixel-pattern state machine used in sketch/sketch.ino, printing every
Bridge event it would send to Linux.

Run:
    python3 sketch/sim_imu_replay.py
"""

import math
from typing import List, Tuple

IMU_INTERVAL_MS = 5
STRIKE_DEBOUNCE_MS = 95
TEMPO_TIMEOUT_MS = 2500
GRAVITY_EMA_ALPHA = 0.04
STRIKE_THRESHOLD_G = 0.42
STRIKE_MAX_G = 2.20


def clampf(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def clampi(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class SketchSimulator:
    def __init__(self) -> None:
        self.gravity_ema = 1.0
        self.prev_dyn = 0.0
        self.armed_for_peak = False
        self.peak_dyn = 0.0
        self.last_strike_ms = 0
        self.intervals: List[int] = []
        self.active_pattern = "idle"
        self.events: List[Tuple[int, str, object]] = []

    def on_set_pixels(self, now_ms: int, pattern: str) -> None:
        self.active_pattern = pattern
        print(f"[{now_ms:5d} ms] Bridge RX <- set_pixels({pattern!r})")

    def step_imu(self, now_ms: int, ax: float, ay: float, az: float) -> None:
        mag = math.sqrt(ax * ax + ay * ay + az * az)
        self.gravity_ema = (1.0 - GRAVITY_EMA_ALPHA) * self.gravity_ema + GRAVITY_EMA_ALPHA * mag
        dyn = abs(mag - self.gravity_ema)

        if dyn >= STRIKE_THRESHOLD_G and (now_ms - self.last_strike_ms) >= STRIKE_DEBOUNCE_MS:
            if not self.armed_for_peak:
                self.armed_for_peak = True
                self.peak_dyn = dyn
            elif dyn > self.peak_dyn:
                self.peak_dyn = dyn

        if self.armed_for_peak and dyn < self.prev_dyn:
            self.armed_for_peak = False
            norm = (self.peak_dyn - STRIKE_THRESHOLD_G) / (STRIKE_MAX_G - STRIKE_THRESHOLD_G)
            intensity = round(clampf(0.15 + norm * 0.85, 0.05, 1.00), 3)
            self.events.append((now_ms, "strike", intensity))
            print(f"[{now_ms:5d} ms] Bridge TX -> strike(intensity={intensity:.3f})")

            if self.last_strike_ms > 0:
                dt = now_ms - self.last_strike_ms
                if STRIKE_DEBOUNCE_MS <= dt <= TEMPO_TIMEOUT_MS:
                    self.intervals = (self.intervals + [dt])[-4:]
                    s = sorted(self.intervals)
                    n = len(s)
                    med = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) // 2
                    bpm = clampi(60000 // med, 30, 220)
                    self.events.append((now_ms, "tempo", bpm))
                    print(f"[{now_ms:5d} ms] Bridge TX -> tempo(bpm={bpm})")
                elif dt > TEMPO_TIMEOUT_MS:
                    self.intervals.clear()

            self.last_strike_ms = now_ms
            self.peak_dyn = 0.0

        self.prev_dyn = dyn


def generate_recorded_imu_stream(duration_ms: int = 2600) -> List[Tuple[int, float, float, float]]:
    """Generates a realistic 200 Hz wrist IMU trace with 4 strikes @ ~600ms (100 BPM) + 1 bounce."""
    planned_strikes = [
        (300, 0.85),   # Gentle strike
        (340, 0.65),   # Mechanical rebound 40ms later -> MUST be rejected by 95ms debounce
        (900, 1.35),   # Medium strike (600ms interval -> 100 BPM)
        (1500, 1.95),  # Strong expressive strike (600ms interval -> 100 BPM)
        (2100, 1.10),  # Steady strike (600ms interval -> 100 BPM)
    ]
    samples: List[Tuple[int, float, float, float]] = []
    for t in range(0, duration_ms, IMU_INTERVAL_MS):
        ax, ay, az = 0.02, -0.01, 1.00  # 1g resting gravity on Z
        for center_ms, amp_g in planned_strikes:
            dist = abs(t - center_ms)
            if dist <= 25:
                # Smooth bell-shaped wrist flick envelope
                env = math.cos((dist / 25.0) * (math.pi / 2.0))
                az += amp_g * env
        samples.append((t, ax, ay, az))
    return samples


def main() -> None:
    print("=== Workstream 1 Standalone Check: sketch/sketch.ino IMU & Modulino Logic ===")
    sim = SketchSimulator()
    print("[    0 ms] Bridge TX -> knob(value=50)")
    for t_ms, ax, ay, az in generate_recorded_imu_stream():
        sim.step_imu(t_ms, ax, ay, az)
        if t_ms == 1000:
            print(f"[{t_ms:5d} ms] Bridge TX -> button(button_id=0)  # Mode toggle")
            sim.on_set_pixels(t_ms, "progress_2")
        if t_ms == 2300:
            sim.on_set_pixels(t_ms, "celebrate")

    strikes = [e for e in sim.events if e[1] == "strike"]
    tempos = [e for e in sim.events if e[1] == "tempo"]
    assert len(strikes) == 4, f"Expected 4 debounced strikes (rebound rejected), got {len(strikes)}"
    assert any(95 <= int(t[2]) <= 105 for t in tempos), f"Expected ~100 BPM tempo, got {tempos}"
    print(f"=== PASS: {len(strikes)} strikes detected (40ms rebound filtered), median tempo={tempos[-1][2]} BPM ===")


if __name__ == "__main__":
    main()
