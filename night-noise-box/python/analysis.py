#!/usr/bin/env python3
"""Measures noise from the mic and uploads a summary every ANALYSIS_SECONDS.

Each upload is one payload in the map's v1 format (see server/static/README.md):

    {"v": 1, "station_id": "box-01", "lat": 60.18, "lon": 24.83, "model": "...",
     "readings": [{"minute": "2026-11-14T22:10:00Z", "duration_s": 10.0,
                   "laeq_db": 48.3, "lmax_db": 61.0, "l90_db": 39.5,
                   "sources": {"traffic": 0.7, "voices": 0.3}, "valid_s": 10.0,
                   "flags": []}]}

`minute` is the reading's start time. The name comes from the map, which was
designed around one reading per minute.

Audio is read in 125 ms blocks. Each block becomes one dB level; LAeq is the
energy mean of those, Lmax the loudest block and L90 the level exceeded 90 % of
the time. Whenever the classifier's input window fills (usually 1 s) it is
classified, and the scores are averaged into `sources`.

Audio only ever sits in two fixed buffers that are overwritten in place and
zeroed on exit. Nothing but the numbers above leaves the box.

    python3 python/analysis.py --self-test
    python3 python/analysis.py --mic synthetic --once   # laptop, no mic
    python3 python/analysis.py --once                   # board, one analysis
"""

from __future__ import annotations

import argparse
import array
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from classifier import Classifier, PlaceholderClassifier, load_classifier  # noqa: E402
from config import (  # noqa: E402
    ANALYSIS_SECONDS,
    CALIBRATION_OFFSET_DB,
    DEVICE_ID,
    DEVICE_LAT,
    DEVICE_LON,
    EI_LABEL_MAP,
    EI_MIN_CONFIDENCE,
    EI_MODEL_PATH,
    MIC_DEVICE,
    MIC_SOURCE,
    READING_SECONDS,
    READINGS_URL,
)
from mic import MicSource, open_mic  # noqa: E402

BLOCKS_PER_SEC = 8
CLIP_LEVEL = 32700
MIN_DB, MAX_DB = 0.0, 120.0


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def block_dbfs(buf: "array.array[int]") -> float:
    """RMS of one block (DC removed) in dBFS, floored at -120."""
    n = len(buf)
    total = sum(buf)
    sq = sum(s * s for s in buf)
    var = max(0.0, sq / n - (total / n) ** 2)
    rms = math.sqrt(var) / 32768.0
    return max(-120.0, 20.0 * math.log10(max(rms, 1e-6)))


def percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class ReadingAccumulator:
    """Collects block levels + classifier scores for one reading, then summarises them."""

    def __init__(self, start: datetime, duration_s: float, label_map: Dict[str, str], min_conf: float) -> None:
        self.start = start
        self.duration_s = duration_s
        self.label_map = label_map
        self.min_conf = min_conf
        self.levels: List[float] = []
        self.clipped_blocks = 0
        self.dropped_blocks = 0
        self.score_sums: Dict[str, float] = {}
        self.inferences = 0

    def add_block(self, level_db: float, clipped: bool) -> None:
        self.levels.append(level_db)
        if clipped:
            self.clipped_blocks += 1

    def add_dropout(self) -> None:
        self.dropped_blocks += 1

    def add_scores(self, scores: Dict[str, float]) -> None:
        if not scores or max(scores.values()) < self.min_conf:
            return
        self.inferences += 1
        for raw, score in scores.items():
            name = self.label_map.get(raw, raw)
            if name:
                self.score_sums[name] = self.score_sums.get(name, 0.0) + score

    def summary(self, flags: List[str]) -> Dict[str, Any]:
        lv = sorted(self.levels)
        laeq = 10.0 * math.log10(sum(10 ** (l / 10.0) for l in lv) / len(lv)) if lv else 0.0
        total = sum(self.score_sums.values())
        sources = (
            {k: round(v / total, 3) for k, v in sorted(self.score_sums.items(), key=lambda kv: -kv[1])}
            if total > 0 else {}
        )
        out_flags = list(flags)
        if lv and self.clipped_blocks / len(lv) > 0.01:
            out_flags.append("clipping")
        if self.dropped_blocks:
            out_flags.append("mic_dropout")
        return {
            "minute": utc_iso(self.start),
            "duration_s": round(self.duration_s, 1),
            "laeq_db": round(laeq, 1),
            "lmax_db": round(lv[-1], 1) if lv else 0.0,
            "l90_db": round(percentile(lv, 0.10), 1),
            "sources": sources,
            "valid_s": round(len(lv) / BLOCKS_PER_SEC, 1),
            "inferences": self.inferences,
            "flags": out_flags,
        }


class NoiseAnalyzer:
    def __init__(
        self,
        mic: MicSource,
        classifier: Classifier,
        calibration_offset_db: float = CALIBRATION_OFFSET_DB,
        analysis_seconds: float = ANALYSIS_SECONDS,
        reading_seconds: float = READING_SECONDS,
        station_id: str = DEVICE_ID,
        lat: float = DEVICE_LAT,
        lon: float = DEVICE_LON,
        label_map: Optional[Dict[str, str]] = None,
        min_conf: float = EI_MIN_CONFIDENCE,
        on_level: Optional[Callable[[float], None]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if mic.sample_rate != classifier.sample_rate:
            raise ValueError(f"mic rate {mic.sample_rate} != model rate {classifier.sample_rate}")
        self.mic = mic
        self.clf = classifier
        self.offset_db = float(calibration_offset_db)
        self.reading_blocks = max(1, int(round(reading_seconds * BLOCKS_PER_SEC)))
        self.readings_per_analysis = max(1, int(round(analysis_seconds / reading_seconds)))
        self.station_id, self.lat, self.lon = station_id, float(lat), float(lon)
        self.label_map = dict(EI_LABEL_MAP if label_map is None else label_map)
        self.min_conf = min_conf
        self.on_level = on_level
        self.clock = clock
        self.flags = ["no_model"] if isinstance(classifier, PlaceholderClassifier) else []
        # The only places audio is kept.
        self._block = array.array("h", [0] * (mic.sample_rate // BLOCKS_PER_SEC))
        self._clf_buf = array.array("h", [0] * classifier.window_samples)
        self._clf_fill = 0
        self.last_level_db = 0.0

    def _feed_classifier(self, acc: ReadingAccumulator) -> None:
        src = self._block
        pos = 0
        while pos < len(src):
            take = min(len(src) - pos, len(self._clf_buf) - self._clf_fill)
            self._clf_buf[self._clf_fill:self._clf_fill + take] = src[pos:pos + take]
            self._clf_fill += take
            pos += take
            if self._clf_fill == len(self._clf_buf):
                self._clf_fill = 0
                acc.add_scores(self.clf.classify(self._clf_buf))

    def _run_reading(self) -> Dict[str, Any]:
        acc = ReadingAccumulator(
            self.clock(), self.reading_blocks / BLOCKS_PER_SEC, self.label_map, self.min_conf
        )
        for _ in range(self.reading_blocks):
            if not self.mic.read_into(self._block):
                acc.add_dropout()
                self._clf_fill = 0  # don't stitch audio across a gap
                continue
            raw = block_dbfs(self._block)
            level = max(MIN_DB, min(MAX_DB, raw + self.offset_db))
            self.last_level_db = level
            acc.add_block(level, max(self._block) >= CLIP_LEVEL or min(self._block) <= -CLIP_LEVEL)
            if self.on_level is not None:
                self.on_level(round(level, 1))
            if self.clf.window_samples:
                self._feed_classifier(acc)
        return acc.summary(self.flags)

    def run_analysis(self) -> Dict[str, Any]:
        """Blocks for one full analysis and returns the v1 payload."""
        readings = [self._run_reading() for _ in range(self.readings_per_analysis)]
        return {
            "v": 1,
            "station_id": self.station_id,
            "lat": self.lat,
            "lon": self.lon,
            "model": self.clf.name,
            "weighting": "Z",  # TODO: A-weighting filter so laeq_db is a true LAeq
            "calibration_offset_db": round(self.offset_db, 1),
            "analysis_seconds": round(self.readings_per_analysis * self.reading_blocks / BLOCKS_PER_SEC, 1),
            "readings": readings,
        }

    def close(self) -> None:
        for buf in (self._block, self._clf_buf):
            for i in range(len(buf)):
                buf[i] = 0
        self.mic.close()
        self.clf.close()


def describe(payload: Dict[str, Any]) -> str:
    parts = []
    for r in payload["readings"]:
        top = ", ".join(f"{k} {v:.0%}" for k, v in list(r["sources"].items())[:3]) or "—"
        parts.append(
            f"{r['minute']}  LAeq {r['laeq_db']:5.1f}  Lmax {r['lmax_db']:5.1f}  L90 {r['l90_db']:5.1f} dB"
            f"  valid {r['valid_s']:.0f}s  sources: {top}  {' '.join(r['flags'])}"
        )
    return "\n".join(parts)


def build_analyzer(
    mic_kind: str = MIC_SOURCE,
    device: str = MIC_DEVICE,
    model_path: Optional[str] = EI_MODEL_PATH,
    **kwargs: Any,
) -> NoiseAnalyzer:
    clf = load_classifier(model_path)
    mic = open_mic(mic_kind, device, clf.sample_rate)
    print(f"[Analysis] mic={mic.device_name} @ {mic.sample_rate} Hz, model={clf.name}")
    return NoiseAnalyzer(mic, clf, **kwargs)


def run_forever(
    analyzer: NoiseAnalyzer,
    readings_url: str = READINGS_URL,
    once: bool = False,
    stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Analyse and upload in a loop. Uploads run on a background thread."""
    from uploader import PayloadUploader

    uploader = PayloadUploader(readings_url) if readings_url else None
    if uploader is None:
        print("[Analysis] READINGS_URL empty — printing results only")
    else:
        print(f"[Analysis] uploading to {readings_url} as {analyzer.station_id!r}")
    try:
        while not (stop and stop()):
            payload = analyzer.run_analysis()
            print(describe(payload), flush=True)
            if uploader is not None:
                uploader.submit(payload)
            if once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if uploader is not None:
            uploader.close()
        analyzer.close()


def run_self_test() -> None:
    from mic import SyntheticMic


    class FakeModel:
        name = "fake-model"
        sample_rate = 16000
        window_samples = 16000
        labels = ["car", "speech", "silence"]
        calls = 0

        def classify(self, buf: "array.array[int]") -> Dict[str, float]:
            FakeModel.calls += 1
            return {"car": 0.6, "speech": 0.3, "silence": 0.1}

        def close(self) -> None:
            return

    an = NoiseAnalyzer(
        SyntheticMic(16000, realtime=False),
        FakeModel(),
        calibration_offset_db=94.0,
        analysis_seconds=10.0,
        reading_seconds=5.0,
        label_map={"car": "traffic", "speech": "voices", "silence": ""},
    )
    p = an.run_analysis()
    json.dumps(p)
    assert p["v"] == 1 and p["station_id"] == DEVICE_ID and len(p["readings"]) == 2
    assert FakeModel.calls == 10, FakeModel.calls  # 10 s of audio / 1 s windows
    for r in p["readings"]:
        assert r["valid_s"] == 5.0 and r["inferences"] == 5
        assert r["lmax_db"] >= r["laeq_db"] >= r["l90_db"] > 0
        assert set(r["sources"]) == {"traffic", "voices"}
        assert abs(sum(r["sources"].values()) - 1.0) < 0.01
        assert "no_model" not in r["flags"]
    # 220 Hz tone at amplitude 6000 ~ -14.7 dBFS -> ~79 dB with a 94 dB offset
    assert 75 < p["readings"][0]["lmax_db"] < 83, p["readings"][0]
    an.close()
    assert not any(an._block) and not any(an._clf_buf), "PCM buffers must be zeroed on close"

    ph = NoiseAnalyzer(SyntheticMic(16000, realtime=False), PlaceholderClassifier(), analysis_seconds=1, reading_seconds=1)
    r = ph.run_analysis()["readings"][0]
    assert r["sources"] == {} and r["flags"] == ["no_model"]
    ph.close()
    print(describe(p))
    print("analysis self-test passed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Night-Noise Box periodic analysis")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--mic", choices=("alsa", "synthetic"), default=MIC_SOURCE)
    ap.add_argument("--device", default=MIC_DEVICE)
    ap.add_argument("--model", default=EI_MODEL_PATH)
    ap.add_argument("--seconds", type=float, default=ANALYSIS_SECONDS, help="Analysis length")
    ap.add_argument("--reading-seconds", type=float, default=None, help="Reading length (default = min(READING_SECONDS, --seconds))")
    ap.add_argument("--readings-url", default=READINGS_URL, help="'' = print only")
    ap.add_argument("--once", action="store_true", help="Run a single analysis and exit")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return
    reading_s = args.reading_seconds or min(READING_SECONDS, args.seconds)
    analyzer = build_analyzer(
        args.mic, args.device, args.model, analysis_seconds=args.seconds, reading_seconds=reading_s
    )
    run_forever(analyzer, args.readings_url, once=args.once)


if __name__ == "__main__":
    main()
