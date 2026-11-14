#!/usr/bin/env python3
"""
Night-Noise Box — Event Uploader (python/uploader.py)
-----------------------------------------------------
Pushes finished event records as JSON to the Noise Map server
(`server/server.py`). Runs on the UNO Q Linux side: the STM32 has no network,
so it reports `event_start` / `event_end` over Bridge and Linux uploads.

Payload (POST `UPLOAD_URL`, `Content-Type: application/json`):

    {
      "device_id": "box-01", "lat": 52.52, "lon": 13.405,
      "events": [ {"id": 7, "start_time": "...", "duration_ms": 2400,
                   "label": "traffic", "confidence": 0.81,
                   "avg_level": 58.2, "peak_level": 66.0,
                   "woke_me": false, "tamper": false, "grid_cell": "B2"} ]
    }

Only scalar event fields are sent — never audio. Uploads happen on a daemon
thread with a bounded queue so a slow or offline server never stalls the
~8 Hz audio loop. Failed batches are retried with backoff; if the queue fills
up, the oldest pending records are dropped (they remain in local SQLite).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional

MAX_PENDING = 500
MAX_BATCH = 50
TIMEOUT_SEC = 5.0
MAX_BACKOFF_SEC = 60.0


class EventUploader:
    def __init__(self, url: str, device_id: str, lat: float, lon: float, verbose: bool = True) -> None:
        self.url = url
        self.device_id = device_id
        self.lat = float(lat)
        self.lon = float(lon)
        self.verbose = verbose
        self._pending: Deque[Dict[str, Any]] = deque(maxlen=MAX_PENDING)
        self._cv = threading.Condition()
        self._stop = False
        self._inflight = 0
        self.sent_count = 0
        self.fail_count = 0
        self.last_error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name="event-uploader", daemon=True)
        self._thread.start()

    def submit(self, record: Dict[str, Any]) -> None:
        """Queue one event record (non-blocking). Re-submitting the same `id` updates it server-side."""
        with self._cv:
            self._pending.append(dict(record))
            self._cv.notify()

    def close(self, flush_timeout: float = 3.0) -> None:
        deadline = time.monotonic() + flush_timeout
        while (self._pending or self._inflight) and time.monotonic() < deadline:
            time.sleep(0.05)
        with self._cv:
            self._stop = True
            self._cv.notify()

    def status(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "pending": len(self._pending) + self._inflight,
            "sent": self.sent_count,
            "failed": self.fail_count,
            "last_error": self.last_error,
        }

    def _post(self, batch: List[Dict[str, Any]]) -> None:
        body = json.dumps(
            {"device_id": self.device_id, "lat": self.lat, "lon": self.lon, "events": batch}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status >= 300:
                raise urllib.error.HTTPError(self.url, resp.status, "upload rejected", resp.headers, None)

    def _run(self) -> None:
        backoff = 1.0
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                batch = [self._pending.popleft() for _ in range(min(MAX_BATCH, len(self._pending)))]
                self._inflight = len(batch)
            try:
                self._post(batch)
            except Exception as exc:  # network down, server offline, bad status ...
                self.fail_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.verbose:
                    print(f"[Uploader] {len(batch)} event(s) not sent ({self.last_error}); retry in {backoff:.0f}s")
                with self._cv:
                    # Put the batch back in front; deque maxlen drops the oldest if full.
                    room = MAX_PENDING - len(self._pending)
                    self._pending.extendleft(reversed(batch[-room:] if room > 0 else []))
                    self._inflight = 0
                    self._cv.wait(timeout=backoff)
                backoff = min(MAX_BACKOFF_SEC, backoff * 2)
                continue
            backoff = 1.0
            self.sent_count += len(batch)
            self.last_error = None
            self._inflight = 0
