#!/usr/bin/env python3
"""
Night-Noise Box — Sound-source classifier (python/classifier.py)
----------------------------------------------------------------
One small interface the analysis loop talks to:

    clf.window_samples   # how many int16 samples one classification needs
    clf.sample_rate      # Hz the model expects (the mic is opened at this rate)
    clf.classify(buf)    # -> {label: score}  (scores 0..1, raw model labels)

Backends
  * `EdgeImpulseClassifier` — runs an Edge Impulse Linux model file (`.eim`)
    through the `edge_impulse_linux` SDK. Drop the exported model at
    `EI_MODEL_PATH` (config.py) and `pip install edge_impulse_linux` on the
    board; it is picked up automatically on the next start. Audio impulses
    take raw int16 samples as features — the DSP block (MFE/MFCC) lives inside
    the `.eim`, so nothing here needs to change when the model is retrained.
  * `PlaceholderClassifier` — used until the model exists. Returns no scores,
    so uploads carry honest empty `sources` plus a `"no_model"` flag instead
    of made-up labels.

    python3 python/classifier.py              # report which backend would load
    python3 python/classifier.py --model x.eim
"""

from __future__ import annotations

import argparse
import array
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Protocol

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from config import EI_MODEL_PATH, SAMPLE_RATE_HZ  # noqa: E402


class Classifier(Protocol):
    name: str
    sample_rate: int
    window_samples: int
    labels: List[str]

    def classify(self, buf: "array.array[int]") -> Dict[str, float]: ...

    def close(self) -> None: ...


class PlaceholderClassifier:
    """Stand-in until the Edge Impulse model is exported. Classifies nothing."""

    name = "placeholder"

    def __init__(self, sample_rate: int = SAMPLE_RATE_HZ, window_seconds: float = 1.0) -> None:
        self.sample_rate = int(sample_rate)
        self.window_samples = int(sample_rate * window_seconds)
        self.labels: List[str] = []

    def classify(self, buf: "array.array[int]") -> Dict[str, float]:
        return {}

    def close(self) -> None:
        return


class EdgeImpulseClassifier:
    """Runs an Edge Impulse Linux `.eim` audio model via `edge_impulse_linux.runner.ImpulseRunner`."""

    def __init__(self, model_path: str) -> None:
        from edge_impulse_linux.runner import ImpulseRunner  # type: ignore

        path = os.path.abspath(model_path)
        if not os.access(path, os.X_OK):
            os.chmod(path, 0o755)  # .eim files are executables; copies often lose +x
        self._runner = ImpulseRunner(path)
        info = self._runner.init()
        params = info["model_parameters"]
        project = info.get("project", {})
        self.name = f"edge-impulse:{project.get('name', Path(path).stem)}#{project.get('deploy_version', '?')}"
        self.sample_rate = int(params["frequency"])
        self.window_samples = int(params["input_features_count"])  # raw samples per inference
        self.labels = list(params.get("labels", []))
        if int(params.get("sensor", 1)) != 1:
            print(f"[Classifier] warning: model sensor={params.get('sensor')} is not a microphone model", file=sys.stderr)

    def classify(self, buf: "array.array[int]") -> Dict[str, float]:
        res = self._runner.classify(buf.tolist())
        scores = res.get("result", {}).get("classification", {})
        return {str(k): float(v) for k, v in scores.items()}

    def close(self) -> None:
        try:
            self._runner.stop()
        except Exception:
            pass


def load_classifier(model_path: Optional[str] = EI_MODEL_PATH, verbose: bool = True) -> Classifier:
    """Edge Impulse model if present and loadable, otherwise the placeholder."""
    reason = "no model path configured"
    if model_path:
        p = Path(model_path)
        if not p.is_absolute():
            p = THIS_DIR.parent / p
        if not p.exists():
            reason = f"model file not found at {p}"
        else:
            try:
                clf = EdgeImpulseClassifier(str(p))
                if verbose:
                    print(f"[Classifier] {clf.name}: {clf.window_samples} samples @ {clf.sample_rate} Hz, labels={clf.labels}")
                return clf
            except ImportError:
                reason = "edge_impulse_linux not installed (pip install edge_impulse_linux)"
            except Exception as exc:
                reason = f"failed to start {p.name}: {type(exc).__name__}: {exc}"
    if verbose:
        print(f"[Classifier] placeholder in use — {reason}")
    return PlaceholderClassifier()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Check which classifier backend loads")
    ap.add_argument("--model", default=EI_MODEL_PATH)
    clf = load_classifier(ap.parse_args().model)
    clf.close()
