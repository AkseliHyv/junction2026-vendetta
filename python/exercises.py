#!/usr/bin/env python3
"""
Workstream 4a: Seated Rehabilitation & Care-Home Preset Exercises (python/exercises.py)
---------------------------------------------------------------------------------------
Plain-data definitions of 4 gentle, seated-accessible movement routines.
All arm heights are normalized in [0.0, 1.0]:
  - 0.15: Hands resting comfortably near lap
  - 0.50: Hands at chest / heart level
  - 0.65: Arms open at shoulder level
  - 0.85: Gentle comfortable reach overhead (seated)

Never shows failure; unreached steps wait patiently while wrist motion plays music.
"""

from __future__ import annotations

import json
from typing import Dict, List, TypedDict


class ExerciseStep(TypedDict):
    left: float
    right: float
    tolerance: float
    hold_sec: float
    instruction: str


class ExercisePreset(TypedDict):
    id: str
    name: str
    steps: List[ExerciseStep]


PRESET_EXERCISES: List[ExercisePreset] = [
    {
        "id": "morning_sun",
        "name": "Morning Sun Reach",
        "steps": [
            {
                "left": 0.20,
                "right": 0.20,
                "tolerance": 0.17,
                "hold_sec": 1.2,
                "instruction": "Rest both hands calmly near your lap",
            },
            {
                "left": 0.55,
                "right": 0.55,
                "tolerance": 0.17,
                "hold_sec": 1.5,
                "instruction": "Float both arms up to shoulder level",
            },
            {
                "left": 0.82,
                "right": 0.82,
                "tolerance": 0.18,
                "hold_sec": 1.5,
                "instruction": "Reach gently toward the warm sun",
            },
            {
                "left": 0.25,
                "right": 0.25,
                "tolerance": 0.17,
                "hold_sec": 1.2,
                "instruction": "Lower both hands softly back down",
            },
        ],
    },
    {
        "id": "wing_lift",
        "name": "Alternating Wing Lift",
        "steps": [
            {
                "left": 0.72,
                "right": 0.20,
                "tolerance": 0.17,
                "hold_sec": 1.4,
                "instruction": "Lift your left arm while resting your right",
            },
            {
                "left": 0.20,
                "right": 0.72,
                "tolerance": 0.17,
                "hold_sec": 1.4,
                "instruction": "Lift your right arm while resting your left",
            },
            {
                "left": 0.68,
                "right": 0.68,
                "tolerance": 0.17,
                "hold_sec": 1.6,
                "instruction": "Open both wings wide together",
            },
        ],
    },
    {
        "id": "gentle_rowing",
        "name": "Gentle Rowing Balance",
        "steps": [
            {
                "left": 0.45,
                "right": 0.45,
                "tolerance": 0.16,
                "hold_sec": 1.3,
                "instruction": "Hold both hands steady at chest height",
            },
            {
                "left": 0.75,
                "right": 0.35,
                "tolerance": 0.17,
                "hold_sec": 1.4,
                "instruction": "Sweep your left hand higher than your right",
            },
            {
                "left": 0.35,
                "right": 0.75,
                "tolerance": 0.17,
                "hold_sec": 1.4,
                "instruction": "Sweep your right hand higher than your left",
            },
            {
                "left": 0.45,
                "right": 0.45,
                "tolerance": 0.16,
                "hold_sec": 1.3,
                "instruction": "Return both hands to a calm center",
            },
        ],
    },
    {
        "id": "crown_and_heart",
        "name": "Crown & Heart Opening",
        "steps": [
            {
                "left": 0.48,
                "right": 0.48,
                "tolerance": 0.17,
                "hold_sec": 1.4,
                "instruction": "Bring both hands gently to heart height",
            },
            {
                "left": 0.85,
                "right": 0.48,
                "tolerance": 0.18,
                "hold_sec": 1.5,
                "instruction": "Raise your left hand high while right stays steady",
            },
            {
                "left": 0.48,
                "right": 0.85,
                "tolerance": 0.18,
                "hold_sec": 1.5,
                "instruction": "Raise your right hand high while left stays steady",
            },
            {
                "left": 0.82,
                "right": 0.82,
                "tolerance": 0.18,
                "hold_sec": 1.6,
                "instruction": "Lift both hands together in celebration",
            },
        ],
    },
]

EXERCISES_BY_ID: Dict[str, ExercisePreset] = {ex["id"]: ex for ex in PRESET_EXERCISES}


if __name__ == "__main__":
    print("=== Workstream 4a Standalone Check: python/exercises.py ===")
    for preset in PRESET_EXERCISES:
        print(json.dumps({"id": preset["id"], "name": preset["name"], "steps": len(preset["steps"])}))
