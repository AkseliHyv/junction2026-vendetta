#!/usr/bin/env python3
"""Uploads JSON to the map server on a background thread.

A slow or offline server never blocks audio processing: items wait in a
bounded queue and failed requests are retried with backoff. When the queue is
full the oldest items are dropped. Event records are still in the local
SQLite database; analysis payloads are lost.

PayloadUploader sends analysis payloads to /api/readings, one per request.
EventUploader sends event records (events mode) to /api/events in batches.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from config import NOISE_MAP_KEY

MAX_PENDING = 500
MAX_BATCH = 50
TIMEOUT_SEC = 5.0
MAX_BACKOFF_SEC = 60.0


class _BackgroundPoster:
    max_batch = MAX_BATCH
    what = "item"

    def __init__(self, url: str, verbose: bool = True) -> None:
        self.url = url
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

    def _body(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError

    def _post(self, batch: List[Dict[str, Any]]) -> None:
        body = json.dumps(self._body(batch)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if NOISE_MAP_KEY:
            headers["Authorization"] = f"Bearer {NOISE_MAP_KEY}"
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
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
                batch = [self._pending.popleft() for _ in range(min(self.max_batch, len(self._pending)))]
                self._inflight = len(batch)
            try:
                self._post(batch)
            except Exception as exc:
                self.fail_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.verbose:
                    print(f"[Uploader] {len(batch)} {self.what}(s) not sent ({self.last_error}); retry in {backoff:.0f}s")
                with self._cv:
                    # Requeue at the front. If full, the deque drops the oldest.
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


class EventUploader(_BackgroundPoster):
    """Re-sending an event with the same id updates it on the server."""

    what = "event"

    def __init__(self, url: str, device_id: str, lat: float, lon: float, verbose: bool = True) -> None:
        self.device_id = device_id
        self.lat = float(lat)
        self.lon = float(lon)
        super().__init__(url, verbose)

    def _body(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"device_id": self.device_id, "lat": self.lat, "lon": self.lon, "events": batch}


class PayloadUploader(_BackgroundPoster):
    max_batch = 1
    what = "analysis"

    def _body(self, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        return batch[0]
