#!/usr/bin/env python3
"""Sound-source classifier.

EdgeImpulseClassifier runs an Edge Impulse Linux model (.eim). The .eim is an
executable that answers JSON requests on a Unix socket; EimRunner talks to it
with the standard library, so nothing needs installing on the board. The
model does its own feature extraction, so retraining needs no code changes.

PlaceholderClassifier is used when there's no model. It returns no scores, so
uploads have empty `sources` and a "no_model" flag rather than made-up labels.

    python3 python/classifier.py              # show which model loads
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
    """Starts `<model.eim> <socket>` and sends it JSON requests. Each reply ends
    with a NUL byte (same protocol as edge_impulse_linux's ImpulseRunner)."""

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
        self.window_samples = int(params["input_features_count"])
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
    """Loads the .eim model, or the placeholder if there is none or it fails to start."""
    reason = "no model path configured"
    if model_path:
        p = Path(model_path)
        if not p.is_absolute():
            p = THIS_DIR.parent / p
        if not p.exists():
            # Use the newest .eim in the folder so a new export works without editing config.py.
            candidates = sorted(p.parent.glob("*.eim"), key=lambda f: (f.stat().st_mtime, f.name))
            if candidates:
                if verbose:
                    print(f"[Classifier] {p.name} not found, using {candidates[-1].name}")
                p = candidates[-1]
        if not p.exists():
            reason = f"no .eim model in {p.parent}"
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
