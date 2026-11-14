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
    with a small stdlib socket client (`EimRunner`), so no pip packages are
    needed on the board. Drop the exported model at `EI_MODEL_PATH`
    (config.py); it is picked up automatically on the next start. Audio impulses
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
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

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


class EimRunner:
    """
    Minimal stdlib client for an Edge Impulse `.eim` model (no pip packages needed).
    The `.eim` is a native executable: started as `<model.eim> <socket path>`, it
    serves JSON requests on that Unix socket and terminates each reply with a NUL
    byte — the same protocol `edge_impulse_linux.runner.ImpulseRunner` speaks.
    """

    def __init__(self, model_path: str, start_timeout: float = 20.0) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="eim-")
        sock_path = os.path.join(self._tmpdir, "runner.sock")
        self._proc = subprocess.Popen(
            [model_path, sock_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        deadline = time.monotonic() + start_timeout
        while not os.path.exists(sock_path):
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() if self._proc.stderr else b"").decode(errors="replace").strip()
                self.stop()
                raise RuntimeError(f"model exited with code {self._proc.returncode}: {err[-300:]}")
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("model did not open its socket in time")
            time.sleep(0.05)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(sock_path)
        self._id = 1

    def _send(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        msg["id"] = self._id
        self._id += 1
        self._sock.sendall(json.dumps(msg).encode("utf-8"))
        data = bytearray()
        while not data.endswith(b"\x00"):
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("model closed the connection")
            data += chunk
        resp = json.loads(bytes(data[:-1]).decode("utf-8"))
        if resp.get("success") is not True:
            raise RuntimeError(f"model error: {resp.get('error', resp)}")
        return resp

    def init(self) -> Dict[str, Any]:
        return self._send({"hello": 1})

    def classify(self, features: List[int]) -> Dict[str, Any]:
        return self._send({"classify": features})

    def stop(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class EdgeImpulseClassifier:
    """Runs an Edge Impulse Linux `.eim` audio model through `EimRunner`."""

    def __init__(self, model_path: str) -> None:
        path = os.path.abspath(model_path)
        if not os.access(path, os.X_OK):
            os.chmod(path, 0o755)  # .eim files are executables; copies often lose +x
        self._runner = EimRunner(path)
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
