#!/usr/bin/env python3
"""
Night-Noise Box — Microphone capture (python/mic.py)
----------------------------------------------------
Reads 16-bit mono PCM from the USB camera's microphone on the UNO Q Linux side.

Backend: `arecord` (alsa-utils, present on the UNO Q Debian image) writing raw
PCM to a pipe that we read straight into a caller-owned fixed-size
`array('h')` buffer. Nothing touches disk; the pipe is the only transport.
`plughw:` lets ALSA resample/downmix so we always get the rate the model wants.

`MIC_DEVICE = "auto"` picks the first ALSA capture card that looks like a USB
camera / USB audio device (see `arecord -l`). Set an explicit device such as
`"plughw:CARD=Camera,DEV=0"` in config.py if auto-detection picks the wrong one.

    python3 python/mic.py --list          # show capture devices + what "auto" picks
    python3 python/mic.py --seconds 3     # print live dBFS for 3 s (no audio kept)

`SyntheticMic` produces a test signal so the analysis pipeline can be run on a
laptop without ALSA.
"""

from __future__ import annotations

import argparse
import array
import math
import random
import re
import shutil
import subprocess
import sys
import time
from typing import List, Optional, Protocol, Tuple

USB_HINTS = ("usb", "camera", "webcam", "cam")


class MicSource(Protocol):
    sample_rate: int
    device_name: str

    def read_into(self, buf: "array.array[int]") -> bool:
        """Fills `buf` completely with int16 samples. Returns False on dropout/EOF."""
        ...

    def close(self) -> None: ...


def list_capture_cards() -> List[Tuple[int, int, str]]:
    """Returns `(card, device, description)` for every ALSA capture device."""
    if shutil.which("arecord") is None:
        return []
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    cards = []
    for line in out.splitlines():
        m = re.match(r"card (\d+): (.*?), device (\d+): (.*)", line.strip())
        if m:
            cards.append((int(m.group(1)), int(m.group(3)), f"{m.group(2)} / {m.group(4)}"))
    return cards


def resolve_device(device: str) -> str:
    """Turns `"auto"` into a concrete `plughw:<card>,<dev>`; passes anything else through."""
    if device != "auto":
        return device
    cards = list_capture_cards()
    for card, dev, desc in cards:
        if any(h in desc.lower() for h in USB_HINTS):
            return f"plughw:{card},{dev}"
    if cards:
        card, dev, _ = cards[0]
        return f"plughw:{card},{dev}"
    return "default"


class ArecordMic:
    """Streams S16_LE mono PCM from `arecord` through a pipe (never a file)."""

    def __init__(self, device: str = "auto", sample_rate: int = 16000) -> None:
        if shutil.which("arecord") is None:
            raise RuntimeError("`arecord` not found (install alsa-utils) — use --mic synthetic on a laptop")
        self.sample_rate = int(sample_rate)
        self.device_name = resolve_device(device)
        self._proc: Optional[subprocess.Popen] = None
        self._start()

    def _start(self) -> None:
        self.close()
        self._proc = subprocess.Popen(
            [
                "arecord", "-q", "-D", self.device_name,
                "-f", "S16_LE", "-c", "1", "-r", str(self.sample_rate),
                "-t", "raw", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def read_into(self, buf: "array.array[int]") -> bool:
        assert self._proc is not None and self._proc.stdout is not None
        view = memoryview(buf).cast("B")
        need = len(view)
        got = 0
        while got < need:
            n = self._proc.stdout.readinto(view[got:])
            if not n:
                err = b""
                if self._proc.stderr is not None:
                    try:
                        err = self._proc.stderr.read() or b""
                    except Exception:
                        pass
                msg = err.decode(errors="replace").strip()
                print(f"[Mic] arecord stopped on {self.device_name}: {msg or 'EOF'}; restarting in 1 s", file=sys.stderr)
                time.sleep(1.0)
                self._start()
                return False
            got += n
        if sys.byteorder != "little":
            buf.byteswap()
        return True

    def close(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=2)
            self._proc = None


class SyntheticMic:
    """Laptop stand-in: quiet noise floor with an occasional louder tone burst, paced in real time."""

    def __init__(self, sample_rate: int = 16000, realtime: bool = True, seed: int = 1) -> None:
        self.sample_rate = int(sample_rate)
        self.device_name = "synthetic"
        self.realtime = realtime
        self._rng = random.Random(seed)
        self._t = 0
        self._next_deadline = time.monotonic()

    def read_into(self, buf: "array.array[int]") -> bool:
        sr = self.sample_rate
        for i in range(len(buf)):
            t = self._t + i
            burst = (t // sr) % 6 in (2, 3)  # 2 s of tone every 6 s
            amp = 6000.0 if burst else 0.0
            s = amp * math.sin(2 * math.pi * 220.0 * t / sr) + self._rng.gauss(0.0, 120.0)
            buf[i] = max(-32768, min(32767, int(s)))
        self._t += len(buf)
        if self.realtime:
            self._next_deadline += len(buf) / sr
            time.sleep(max(0.0, self._next_deadline - time.monotonic()))
        return True

    def close(self) -> None:
        return


def open_mic(kind: str, device: str, sample_rate: int) -> MicSource:
    if kind == "synthetic":
        return SyntheticMic(sample_rate)
    return ArecordMic(device, sample_rate)


def main() -> None:
    ap = argparse.ArgumentParser(description="Night-Noise Box microphone check")
    ap.add_argument("--list", action="store_true", help="List ALSA capture devices")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--mic", choices=("alsa", "synthetic"), default="alsa")
    args = ap.parse_args()

    if args.list:
        cards = list_capture_cards()
        if not cards:
            print("No ALSA capture devices found (is arecord installed / mic plugged in?)")
        for card, dev, desc in cards:
            print(f"  plughw:{card},{dev}   {desc}")
        print(f"auto -> {resolve_device('auto')}")
        return

    mic = open_mic(args.mic, args.device, args.rate)
    print(f"[Mic] reading {mic.device_name} @ {mic.sample_rate} Hz")
    buf = array.array("h", [0] * (args.rate // 8))
    try:
        for _ in range(int(args.seconds * 8)):
            if not mic.read_into(buf):
                continue
            rms = math.sqrt(sum(s * s for s in buf) / len(buf)) / 32768.0
            print(f"  {20 * math.log10(max(rms, 1e-6)):6.1f} dBFS")
    finally:
        mic.close()


if __name__ == "__main__":
    main()
