#!/usr/bin/env python3
"""
Workstream 2: Seated Arm Pose Estimation (python/pose.py)
---------------------------------------------------------
Captures frames from a USB camera (or video file / synthetic generator for laptop
testing), estimates seated left/right arm height normalized to 0.0 .. 1.0, and
tracks a `person_present` flag.

STRICT PRIVACY GUARANTEE:
- Frames are processed purely in volatile memory inside `_read_camera_once()`.
- Frames are NEVER written to disk and NEVER returned or exposed outside this module.

Standalone check:
    python3 python/pose.py --fake --steps 12
    python3 python/pose.py --webcam --steps 30
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from typing import Optional, Union


def _clamp01(val: float) -> float:
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


@dataclass(frozen=True)
class PoseReading:
    left: float            # 0.0 (hand at lap) .. 1.0 (hand comfortably overhead)
    right: float           # 0.0 (hand at lap) .. 1.0 (hand comfortably overhead)
    person_present: bool   # True when seated upper-body landmarks are visible
    fps: float             # Measured pose loop rate
    source: str            # "mediapipe_camera" | "fake_seated" | "camera_unavailable"


class SeatedArmPoseEstimator:
    """
    Smoothed seated upper-body arm-level estimator.
    Maps wrist elevation relative to shoulder/torso geometry into [0.0, 1.0]:
      - 0.00: Wrist resting near lap (approx. 1.15 torso-lengths below shoulder)
      - 0.55: Wrist level with shoulder
      - 1.00: Wrist raised comfortably above head (approx. 0.85 torso-lengths above shoulder)
    """

    def __init__(
        self,
        camera_source: Union[int, str] = 0,
        use_fake: bool = False,
        smoothing_alpha: float = 0.35,
        width: int = 320,
        height: int = 240,
    ) -> None:
        self.camera_source = camera_source
        self.use_fake = use_fake
        self.alpha = smoothing_alpha
        self.width = width
        self.height = height

        self._left_smooth = 0.20
        self._right_smooth = 0.20
        self._initialized_ema = False
        self._last_tick = time.monotonic()
        self._fps_ema = 0.0
        self._start_mono = time.monotonic()

        self._cap = None
        self._mp_pose = None
        self._source_name = "fake_seated" if use_fake else "camera_unavailable"

        if not self.use_fake:
            self._init_camera_and_model()

    def _init_camera_and_model(self) -> None:
        try:
            import cv2  # type: ignore
            import mediapipe as mp  # type: ignore

            cap = cv2.VideoCapture(self.camera_source)
            if not cap.isOpened():
                cap.release()
                self._source_name = "camera_unavailable"
                return

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # model_complexity=0 (Lite) maximizes FPS on UNO Q quad-core Cortex-A53
            self._mp_pose = mp.solutions.pose.Pose(
                static_image_mode=False,
                model_complexity=0,
                smooth_landmarks=True,
                enable_segmentation=False,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._cap = cap
            self._cv2 = cv2
            self._source_name = "mediapipe_camera"
        except Exception:
            # Clean fallback when cv2/mediapipe or physical webcam is absent
            self._cap = None
            self._mp_pose = None
            self._source_name = "camera_unavailable"

    def _update_fps(self) -> float:
        now = time.monotonic()
        dt = max(1e-4, now - self._last_tick)
        self._last_tick = now
        inst_fps = 1.0 / dt
        if self._fps_ema <= 0.0:
            self._fps_ema = inst_fps
        else:
            self._fps_ema = 0.85 * self._fps_ema + 0.15 * inst_fps
        return round(self._fps_ema, 1)

    def _smooth(self, raw_left: float, raw_right: float) -> tuple[float, float]:
        raw_left = _clamp01(raw_left)
        raw_right = _clamp01(raw_right)
        if not self._initialized_ema:
            self._left_smooth = raw_left
            self._right_smooth = raw_right
            self._initialized_ema = True
        else:
            self._left_smooth = (1.0 - self.alpha) * self._left_smooth + self.alpha * raw_left
            self._right_smooth = (1.0 - self.alpha) * self._right_smooth + self.alpha * raw_right
        return round(self._left_smooth, 3), round(self._right_smooth, 3)

    def set_simulated_levels(self, left: float, right: float, present: bool = True) -> PoseReading:
        """Used by laptop tests to inject exact simulated seated arm levels."""
        self.use_fake = True
        self._source_name = "fake_seated"
        sm_l, sm_r = self._smooth(left, right)
        return PoseReading(
            left=sm_l,
            right=sm_r,
            person_present=present,
            fps=self._update_fps(),
            source=self._source_name,
        )

    def read(self) -> PoseReading:
        """
        Captures and processes one frame (or synthetic tick) and returns ONLY
        numeric arm heights and presence. Raw pixels never leave this method.
        """
        if self.use_fake:
            t = time.monotonic() - self._start_mono
            # Gentle seated wave across the pentatonic range
            raw_l = 0.50 + 0.38 * math.sin(t * 1.15)
            raw_r = 0.50 + 0.38 * math.sin(t * 0.85 + 0.9)
            sm_l, sm_r = self._smooth(raw_l, raw_r)
            return PoseReading(
                left=sm_l,
                right=sm_r,
                person_present=True,
                fps=self._update_fps(),
                source="fake_seated",
            )

        if self._cap is None or self._mp_pose is None:
            return PoseReading(
                left=round(self._left_smooth, 3),
                right=round(self._right_smooth, 3),
                person_present=False,
                fps=0.0,
                source="camera_unavailable",
            )

        ok, frame_bgr = self._cap.read()
        if not ok or frame_bgr is None:
            return PoseReading(
                left=round(self._left_smooth, 3),
                right=round(self._right_smooth, 3),
                person_present=False,
                fps=self._update_fps(),
                source=self._source_name,
            )

        try:
            frame_rgb = self._cv2.cvtColor(frame_bgr, self._cv2.COLOR_BGR2RGB)
            frame_rgb.flags.writeable = False
            results = self._mp_pose.process(frame_rgb)
        finally:
            # Explicitly drop references to pixel buffers immediately
            del frame_bgr

        if not results or not results.pose_landmarks:
            return PoseReading(
                left=round(self._left_smooth, 3),
                right=round(self._right_smooth, 3),
                person_present=False,
                fps=self._update_fps(),
                source=self._source_name,
            )

        lm = results.pose_landmarks.landmark
        # MediaPipe Pose indices:
        # 11: left_shoulder, 12: right_shoulder, 15: left_wrist, 16: right_wrist
        l_sh, r_sh = lm[11], lm[12]
        l_wr, r_wr = lm[15], lm[16]

        shoulders_visible = (l_sh.visibility >= 0.45) and (r_sh.visibility >= 0.45)
        wrists_visible = (l_wr.visibility >= 0.35) or (r_wr.visibility >= 0.35)
        if not (shoulders_visible and wrists_visible):
            return PoseReading(
                left=round(self._left_smooth, 3),
                right=round(self._right_smooth, 3),
                person_present=False,
                fps=self._update_fps(),
                source=self._source_name,
            )

        # Normalize arm elevation by shoulder width so distance from camera (1.2m..2.5m)
        # does not skew seated arm height. Note: image Y increases downward.
        shoulder_span = max(0.10, math.hypot(l_sh.x - r_sh.x, l_sh.y - r_sh.y))
        lap_offset = 1.25 * shoulder_span       # Wrist resting on lap below shoulder
        overhead_offset = 0.95 * shoulder_span  # Wrist comfortably raised overhead
        total_span = lap_offset + overhead_offset

        # Mirror-friendly mapping: player's own left/right arm
        raw_left = ((l_sh.y + lap_offset) - l_wr.y) / total_span if l_wr.visibility >= 0.35 else self._left_smooth
        raw_right = ((r_sh.y + lap_offset) - r_wr.y) / total_span if r_wr.visibility >= 0.35 else self._right_smooth

        sm_l, sm_r = self._smooth(raw_left, raw_right)
        return PoseReading(
            left=sm_l,
            right=sm_r,
            person_present=True,
            fps=self._update_fps(),
            source=self._source_name,
        )

    def close(self) -> None:
        if self._mp_pose is not None:
            try:
                self._mp_pose.close()
            except Exception:
                pass
            self._mp_pose = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Seated arm pose estimator standalone runner")
    parser.add_argument("--fake", action="store_true", help="Generate smooth synthetic seated arm motion")
    parser.add_argument("--webcam", type=int, default=0, help="USB camera index (default 0)")
    parser.add_argument("--video", type=str, default=None, help="Optional local video file for offline testing")
    parser.add_argument("--steps", type=int, default=10, help="Number of readings to print (0 = run until Ctrl+C)")
    args = parser.parse_args()

    cam_src: Optional[Union[int, str]] = args.video if args.video else args.webcam
    estimator = SeatedArmPoseEstimator(camera_source=cam_src or 0, use_fake=args.fake)

    print(f"=== Workstream 2 Standalone Check: python/pose.py (source={estimator._source_name}) ===")
    count = 0
    try:
        while args.steps == 0 or count < args.steps:
            reading = estimator.read()
            print(
                json.dumps(
                    {
                        "left": reading.left,
                        "right": reading.right,
                        "person_present": reading.person_present,
                        "fps": reading.fps,
                        "source": reading.source,
                    }
                )
            )
            count += 1
            time.sleep(0.066)
    except KeyboardInterrupt:
        pass
    finally:
        estimator.close()


if __name__ == "__main__":
    main()
