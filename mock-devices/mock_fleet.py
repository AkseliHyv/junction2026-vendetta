#!/usr/bin/env python3
"""A fleet of mock Night-Noise Boxes that upload to the map server.

Each mock box sends the same v1 analysis payload a real box sends
(night-noise-box/python/analysis.py), to the same POST /api/readings, on the
same schedule (one 10 s reading every 10 s by default). The numbers come from
a simulation of the sound at the box's site; see acoustics.py.

Like a real box, a mock box keeps measuring while its Wi-Fi is down or the
server is unreachable: payloads wait in a queue of at most 500 and are resent
in order, with backoff, when the connection comes back. A power outage loses
the data instead.

    python3 mock-devices/mock_fleet.py                         # 36 boxes -> http://localhost:8080
    python3 mock-devices/mock_fleet.py --generate 500          # plus 500 generated ones
    python3 mock-devices/mock_fleet.py --backfill-hours 24     # first send a day of history
    python3 mock-devices/mock_fleet.py --shard 0/4             # 1 of 4 processes, for big fleets
    python3 mock-devices/mock_fleet.py --preview mock-0006-iso-roobertinkatu
    python3 mock-devices/mock_fleet.py --city --processes 4    # ~3000 boxes on real buildings (city/)

Fleet status and control on http://localhost:8090 (see README.md).
Standard library only.
"""

from __future__ import annotations

import argparse
import heapq
import http.client
import json
import math
import random
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from acoustics import LOCAL_TZ, PROFILES, SiteSound, Weather

HERE = Path(__file__).resolve().parent
CITY_DIR = HERE / "city"
MAX_PENDING = 500  # same as night-noise-box/python/uploader.py
TIMEOUT_SEC = 5.0
MAX_BACKOFF_SEC = 60.0
MODEL_NAME = "edge-impulse:night-noise#2"

# Profile mix for --generate: mostly homes and streets, a few special sites.
GENERATED_MIX = {
    "residential_street": 20, "suburban": 14, "courtyard": 10, "arterial": 9, "arterial_tram": 6,
    "park": 6, "school": 5, "motorway": 4, "construction": 4, "forest_park": 3, "mall_hub": 3,
    "nightlife": 3, "campus": 2, "rail": 2, "station": 2, "shore": 2, "plaza": 2, "rural": 2,
    "transit_hub": 1, "airport": 0.5,
}


# --- Devices ------------------------------------------------------------------

def _h(*key: Any) -> float:
    return zlib.crc32("|".join(str(k) for k in key).encode()) / 2**32


class MockDevice:
    def __init__(self, spec: Dict[str, Any], weather: Weather, seed: int, world: Any = None) -> None:
        self.id = str(spec["id"])
        self.lat = float(spec["lat"])
        self.lon = float(spec["lon"])
        self.seed = seed
        city = world is not None and "roads" in spec
        self.profile = f"city:{spec['kind']}" if city else str(spec["profile"])
        if not city and self.profile not in PROFILES:
            raise ValueError(f"{self.id}: unknown profile {self.profile!r} (have: {', '.join(PROFILES)})")
        self.offset_db = float(spec.get("calibration_offset_db", 94.0))
        # City boxes: about 2 % still run firmware without the classifier.
        self.has_model = not spec.get("no_model", city and _h("nomodel", self.id) < 0.02)
        self.wifi_per_day = float(spec.get("wifi_outages_per_day", 0.5))
        self.power_per_day = float(spec.get("power_outages_per_day", 0.05))
        dev_seed = zlib.crc32(self.id.encode()) ^ seed
        self.rng = random.Random(dev_seed ^ 0xA5A5)
        kw = dict(
            level_bias_db=float(spec.get("level_bias_db", 0.0)),
            cal_error_db=float(spec.get("cal_error_db", round(self.rng.gauss(0, 0.8), 1))),
            clip_db=self.offset_db,
            has_model=self.has_model,
        )
        if city:
            from world import CitySound
            self.sound: SiteSound = CitySound(spec, world, seed=dev_seed, **kw)
        else:
            self.sound = SiteSound(PROFILES[self.profile], weather, dev_seed, **kw)

        self.lock = threading.Lock()
        self.outbox: Deque[Dict[str, Any]] = deque()
        self.flushing = False
        self.retry_at = 0.0
        self.backoff = 1.0
        self.wifi_down_until = 0.0
        self.power_off_until = 0.0
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error: Optional[str] = None
        self.last_reading: Optional[Dict[str, Any]] = None
        self.last_sent_at: Optional[float] = None

    # Payload exactly as night-noise-box/python/analysis.py builds it.
    def payload(self, readings: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "v": 1,
            "station_id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "model": MODEL_NAME if self.has_model else "placeholder",
            "weighting": "Z",
            "calibration_offset_db": round(self.offset_db, 1),
            "analysis_seconds": round(sum(r["duration_s"] for r in readings), 1),
            "readings": readings,
        }

    def measure(self, t_start: float, analysis_s: float, reading_s: float, block_s: float = 0.125) -> Dict[str, Any]:
        n = max(1, int(analysis_s / reading_s + 1e-6))
        readings = [self.sound.reading(t_start + i * reading_s, reading_s, block_s) for i in range(n)]
        rest = analysis_s - n * reading_s
        if rest >= 1.0:  # a window that isn't a whole number of readings (backfill handing over to live)
            readings.append(self.sound.reading(t_start + n * reading_s, rest, block_s))
        self.last_reading = readings[-1]
        return self.payload(readings)

    def queue(self, payload: Dict[str, Any]) -> None:
        with self.lock:
            if len(self.outbox) >= MAX_PENDING:
                self.outbox.popleft()  # full: drop the oldest, like the real uploader
                self.dropped += 1
            self.outbox.append(payload)

    def state(self, now: float) -> str:
        if now < self.power_off_until:
            return "power_off"
        if now < self.wifi_down_until:
            return "wifi_down"
        return "online"

    def maybe_fail(self, now: float, interval: float) -> Optional[Tuple[str, float]]:
        """Random outages. Returns (kind, until) when one starts."""
        if self.state(now) != "online":
            return None
        district = district_outage(now, self.seed)
        if district and math.hypot((self.lat - district[0]) * 111.0, (self.lon - district[1]) * 55.4) < district[2]:
            kind, until = district[3], district[4]
            if kind == "power":
                self.power_off_until = until
                self.sound.events.clear()
            else:
                self.wifi_down_until = until
            return f"district {kind}", until
        if self.rng.random() < self.power_per_day * interval / 86400:
            until = now + 60 * self.rng.lognormvariate(3.4, 0.8)  # median ~30 min
            self.power_off_until = until
            self.sound.events.clear()
            return "power", until
        if self.rng.random() < self.wifi_per_day * interval / 86400:
            until = now + 60 * self.rng.lognormvariate(2.3, 0.9)  # median ~10 min
            self.wifi_down_until = until
            return "wifi", until
        return None

    def status(self, now: float) -> Dict[str, Any]:
        r = self.last_reading or {}
        top = next(iter(r.get("sources") or {}), None)
        return {
            "id": self.id, "profile": self.profile, "lat": self.lat, "lon": self.lon,
            "state": self.state(now), "pending": len(self.outbox), "sent": self.sent,
            "failed": self.failed, "dropped": self.dropped, "last_error": self.last_error,
            "last_sent_at": self.last_sent_at, "laeq_db": r.get("laeq_db"), "l90_db": r.get("l90_db"),
            "lmax_db": r.get("lmax_db"), "top_source": top, "flags": r.get("flags"),
        }


def district_outage(t: float, seed: int) -> Optional[Tuple[float, float, float, str, float]]:
    """An outage covering a whole area (power cut, operator fault): about one every two days.
    (lat, lon, radius km, kind, until) while one is on. The same in every shard."""
    slot = int(t // 3600)
    if _h(seed, "district", slot) >= 0.02:
        return None
    start = slot * 3600 + _h(seed, "d-start", slot) * 1800
    until = start + 60 * (20 + 70 * _h(seed, "d-len", slot))
    if not start <= t < until:
        return None
    lat = 60.15 + 0.13 * _h(seed, "d-lat", slot)
    lon = 24.62 + 0.50 * _h(seed, "d-lon", slot)
    kind = "power" if _h(seed, "d-kind", slot) < 0.5 else "wifi"
    return lat, lon, 0.6 + 1.2 * _h(seed, "d-r", slot), kind, until


def load_city(seed: int, weather: Weather) -> Tuple[List[Dict[str, Any]], Any]:
    devices_path, emitters_path = CITY_DIR / "devices.json", CITY_DIR / "emitters.json"
    if not devices_path.exists():
        raise SystemExit(f"{devices_path} missing: run city/osm_fetch.py and city/build_city.py")
    sys.path.insert(0, str(CITY_DIR))
    from world import World
    world = World(json.loads(emitters_path.read_text()), weather, seed)
    return json.loads(devices_path.read_text())["devices"], world


def load_specs(path: Path, generate: int, seed: int) -> List[Dict[str, Any]]:
    specs = list(json.loads(path.read_text())["devices"])
    ids = {s["id"] for s in specs}
    if len(ids) != len(specs):
        raise ValueError(f"{path}: duplicate device ids")
    if generate:
        rng = random.Random(seed ^ 0x6E6E)
        anchors = [(s["lat"], s["lon"]) for s in specs] or [(60.1699, 24.9384)]
        names, weights = zip(*GENERATED_MIX.items())
        for k in range(len(specs) + 1, len(specs) + generate + 1):
            alat, alon = rng.choice(anchors)
            profile = rng.choices(names, weights)[0]
            specs.append({
                "id": f"mock-{k:04d}-{profile.replace('_', '-')}",
                "lat": round(alat + rng.gauss(0, 0.010), 5),  # ~1 km around an existing box
                "lon": round(alon + rng.gauss(0, 0.020), 5),
                "profile": profile,
                "level_bias_db": round(rng.gauss(0, 2.0), 1),
                "no_model": rng.random() < 0.03,
                "wifi_outages_per_day": round(rng.choice([0.2, 0.5, 0.5, 1, 3]), 1),
            })
    return specs


# --- Uploading ----------------------------------------------------------------

class Sender:
    """POSTs payloads to /api/readings. One keep-alive connection per thread."""

    def __init__(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            raise ValueError(f"bad --server URL {url!r}")
        self.scheme, self.host, self.port = u.scheme, u.hostname or "localhost", u.port
        self.path = u.path.rstrip("/")
        if not self.path.endswith("/api/readings"):
            self.path += "/api/readings"
        self.url = f"{u.scheme}://{u.netloc}{self.path}"
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        c = getattr(self._local, "conn", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            c = self._local.conn = cls(self.host, self.port, timeout=TIMEOUT_SEC)
        return c

    def post(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        conn = self._conn()
        try:
            conn.request("POST", self.path, body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except Exception:
            conn.close()
            self._local.conn = None
            raise
        if resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}: {data[:200].decode('utf-8', 'replace')}")


class Fleet:
    def __init__(self, devices: List[MockDevice], sender: Sender, analysis_s: float, reading_s: float,
                 senders: int, verbose: bool) -> None:
        self.devices = devices
        self.by_id = {d.id: d for d in devices}
        self.sender = sender
        self.analysis_s = analysis_s
        self.reading_s = reading_s
        self.verbose = verbose
        self.pool = ThreadPoolExecutor(max_workers=senders, thread_name_prefix="sender")
        self._heap: List[Tuple[float, int, str, MockDevice]] = []
        self._seq = 0
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self.started = time.time()
        self.lag = 0.0

    # Scheduling
    def _push(self, due: float, kind: str, dev: MockDevice) -> None:
        with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, (due, self._seq, kind, dev))
            self._cv.notify()

    def kick(self, dev: MockDevice, now: Optional[float] = None) -> None:
        """Start flushing the device's queue if it can send."""
        now = time.time() if now is None else now
        with dev.lock:
            if dev.flushing or not dev.outbox or dev.state(now) != "online" or now < dev.retry_at:
                return
            dev.flushing = True
        self.pool.submit(self._flush, dev)

    def _flush(self, dev: MockDevice) -> None:
        while not self._stop.is_set():
            now = time.time()
            with dev.lock:
                if not dev.outbox or dev.state(now) != "online":
                    dev.flushing = False
                    return
                payload = dev.outbox[0]
            try:
                self.sender.post(payload)
            except Exception as exc:
                with dev.lock:
                    dev.failed += 1
                    dev.last_error = f"{type(exc).__name__}: {exc}"
                    dev.retry_at = time.time() + dev.backoff
                    dev.backoff = min(MAX_BACKOFF_SEC, dev.backoff * 2)
                    dev.flushing = False
                    retry = dev.retry_at
                if self.verbose:
                    print(f"[{dev.id}] upload failed ({dev.last_error}); retry in {dev.backoff / 2:.0f}s", flush=True)
                self._push(retry, "kick", dev)
                return
            with dev.lock:
                if dev.outbox and dev.outbox[0] is payload:
                    dev.outbox.popleft()
                dev.sent += 1
                dev.backoff = 1.0
                dev.last_error = None
                dev.last_sent_at = time.time()
        with dev.lock:
            dev.flushing = False

    def outage(self, dev: MockDevice, kind: str, minutes: float) -> None:
        until = time.time() + minutes * 60
        if kind == "power":
            dev.power_off_until = until
            dev.sound.events.clear()
        else:
            dev.wifi_down_until = until
        self._push(until + 0.5, "kick", dev)

    def restore(self, dev: MockDevice) -> None:
        dev.power_off_until = dev.wifi_down_until = dev.retry_at = 0.0
        dev.backoff = 1.0
        self.kick(dev)

    def backfill(self, hours: float, end: Optional[float] = None) -> None:
        """Send `hours` of history: 15 one-minute readings per payload, like the map's original design.

        Up to `end` if given (a live process takes over from there), else up to now."""
        now = time.time() if end is None else end
        start = int((now - hours * 3600) // 900) * 900
        t0 = time.monotonic()
        done = [start]  # every box is backfilled up to here

        def fill(end: int) -> None:
            # Window by window over all boxes (not box by box), so boxes near each other
            # reuse the same cars and trams from the World cache while they are still in it.
            last = time.monotonic()
            t = done[0]
            while t < end:
                win_end = min(end, (t // 900 + 1) * 900)
                for dev in self.devices:
                    # 2.5 s blocks: same LAeq/L90 as 1 s within a few tenths of a dB, 2-3x faster.
                    dev.queue(dev.measure(t, win_end - t, 60.0, block_s=2.5))
                    self.kick(dev)  # upload while the next window is computed
                t = done[0] = win_end
                if time.monotonic() - last > 30:
                    last = time.monotonic()
                    print(f"[Fleet] backfill: {(t - start) / 3600:.1f}/{(end - start) / 3600:.1f} h", flush=True)
            for dev in self.devices:
                dev.sound.events = [e for e in dev.sound.events if e.end > end]

        if end is not None:
            fill(int(end))
        else:
            fill(int(now // 60) * 60)
            # The minutes that passed meanwhile, so live readings carry on without a gap.
            fill(int(time.time() // 60) * 60)
        print(f"[Fleet] backfill: {hours:g} h x {len(self.devices)} boxes "
              f"generated in {time.monotonic() - t0:.0f}s", flush=True)
        for dev in self.devices:
            self.kick(dev)

    def drain(self) -> None:
        """Wait until every queued payload is sent, retrying failed uploads (without run())."""
        while not self._stop.is_set() and any(d.outbox for d in self.devices):
            now = time.time()
            for dev in self.devices:
                self.kick(dev, now)
            self._stop.wait(1.0)

    def run(self) -> None:
        now = time.time()
        rng = random.Random(len(self.devices))
        for dev in self.devices:  # boxes boot at different moments
            self._push(now + rng.uniform(0.5, self.analysis_s), "measure", dev)
        threading.Thread(target=self._report_loop, name="report", daemon=True).start()
        while not self._stop.is_set():
            with self._cv:
                while not self._heap or self._heap[0][0] > time.time():
                    if self._stop.is_set():
                        return
                    wait = (self._heap[0][0] - time.time()) if self._heap else 1.0
                    self._cv.wait(timeout=max(0.0, min(wait, 1.0)))
                due, _, kind, dev = heapq.heappop(self._heap)
            now = time.time()
            if kind == "kick":
                self.kick(dev, now)
                continue
            self.lag = now - due
            self._push(due + self.analysis_s, "measure", dev)
            failure = dev.maybe_fail(due, self.analysis_s)
            if failure:
                kind_, until = failure
                if self.verbose:
                    print(f"[{dev.id}] {kind_} outage until {datetime.fromtimestamp(until, LOCAL_TZ):%H:%M}", flush=True)
                self._push(until + 0.5, "kick", dev)
            if dev.state(due) == "power_off":
                continue
            # The box measured [due - analysis_s, due) and uploads at `due`.
            dev.queue(dev.measure(due - self.analysis_s, self.analysis_s, self.reading_s))
            self.kick(dev, now)

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def totals(self) -> Dict[str, Any]:
        now = time.time()
        states: Dict[str, int] = {}
        for d in self.devices:
            s = d.state(now)
            states[s] = states.get(s, 0) + 1
        return {
            "server": self.sender.url, "devices": len(self.devices), "states": states,
            "sent": sum(d.sent for d in self.devices), "failed": sum(d.failed for d in self.devices),
            "pending": sum(len(d.outbox) for d in self.devices), "dropped": sum(d.dropped for d in self.devices),
            "scheduler_lag_s": round(self.lag, 2), "uptime_s": round(now - self.started),
            "analysis_seconds": self.analysis_s, "reading_seconds": self.reading_s,
        }

    def _report_loop(self, every: float = 30.0) -> None:
        last_sent, last_t = 0, time.time()
        while not self._stop.wait(every):
            t = self.totals()
            now = time.time()
            rate = (t["sent"] - last_sent) / (now - last_t)
            last_sent, last_t = t["sent"], now
            errors = {d.last_error for d in self.devices if d.last_error}
            states = ", ".join(f"{v} {k}" for k, v in sorted(t["states"].items()))
            msg = (f"[Fleet] {states} | {rate:.1f} uploads/s, {t['sent']} sent, {t['pending']} queued, "
                   f"{t['failed']} failed, {t['dropped']} dropped")
            if self.lag > 2.0:
                msg += f" | scheduler {self.lag:.1f}s behind: split the fleet with --shard"
            if errors:
                msg += f" | last error: {sorted(errors)[0]}"
            print(msg, flush=True)


# --- Status / control server --------------------------------------------------

STATUS_PAGE = """<!doctype html><meta charset="utf-8"><title>Mock fleet</title>
<style>body{font:13px system-ui;margin:16px;background:#111;color:#ddd}table{border-collapse:collapse}
td,th{padding:3px 8px;border-bottom:1px solid #333;text-align:left}.online{color:#6c6}.wifi_down{color:#fc6}
.power_off{color:#f66}code{color:#9cf}</style>
<h3>Mock fleet</h3><div id="t"></div><table><thead><tr><th>id</th><th>profile</th><th>state</th><th>LAeq</th>
<th>L90</th><th>Lmax</th><th>top source</th><th>flags</th><th>sent</th><th>queued</th><th>failed</th></tr></thead>
<tbody id="b"></tbody></table>
<p>POST <code>/api/devices/&lt;id|all&gt;/outage?kind=wifi|power&amp;minutes=10</code>,
<code>/api/devices/&lt;id|all&gt;/restore</code></p>
<script>
async function tick(){
  const [s,d]=await Promise.all([fetch('/api/status').then(r=>r.json()),fetch('/api/devices').then(r=>r.json())]);
  document.getElementById('t').textContent=`${s.devices} boxes -> ${s.server} | ${JSON.stringify(s.states)} | sent ${s.sent}, queued ${s.pending}, failed ${s.failed}`;
  document.getElementById('b').innerHTML=d.devices.map(x=>`<tr><td>${x.id}</td><td>${x.profile}</td>
  <td class="${x.state}">${x.state}</td><td>${x.laeq_db??''}</td><td>${x.l90_db??''}</td><td>${x.lmax_db??''}</td>
  <td>${x.top_source??''}</td><td>${(x.flags||[]).join(' ')}</td><td>${x.sent}</td><td>${x.pending}</td><td>${x.failed}</td></tr>`).join('');
}
tick();setInterval(tick,5000);
</script>"""


def make_status_handler(fleet: Fleet):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = STATUS_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/status":
                self._json(200, fleet.totals())
            elif path == "/api/devices":
                now = time.time()
                self._json(200, {"devices": [d.status(now) for d in fleet.devices]})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "devices"] or parts[3] not in ("outage", "restore"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            target, action = parts[2], parts[3]
            devs = fleet.devices if target == "all" else [fleet.by_id[target]] if target in fleet.by_id else []
            if not devs:
                self._json(404, {"ok": False, "error": f"no device {target!r}"})
                return
            qs = parse_qs(url.query)
            if action == "outage":
                kind = qs.get("kind", ["wifi"])[0]
                try:
                    minutes = float(qs.get("minutes", ["10"])[0])
                except ValueError:
                    minutes = -1
                if kind not in ("wifi", "power") or minutes <= 0:
                    self._json(400, {"ok": False, "error": "kind must be wifi|power, minutes > 0"})
                    return
                for d in devs:
                    fleet.outage(d, kind, minutes)
            else:
                for d in devs:
                    fleet.restore(d)
            self._json(200, {"ok": True, "devices": len(devs)})

    return Handler


# --- CLI ----------------------------------------------------------------------

def preview(dev: MockDevice, hours: float) -> None:
    """Print hourly LAeq/L90/Lmax and top source for the last `hours`, without sending anything."""
    import math
    end = int(time.time() // 3600) * 3600
    print(f"{dev.id} ({dev.profile}) @ {dev.lat},{dev.lon}")
    for h in range(int(hours), 0, -1):
        t0 = end - h * 3600
        rs = [dev.sound.reading(t0 + m * 60, 60.0, 1.0) for m in range(60)]
        leq = 10 * math.log10(sum(10 ** (r["laeq_db"] / 10) for r in rs) / len(rs))
        src: Dict[str, float] = {}
        for r in rs:
            for k, v in r["sources"].items():
                src[k] = src.get(k, 0) + v / len(rs)
        top = ", ".join(f"{k} {v:.0%}" for k, v in sorted(src.items(), key=lambda kv: -kv[1])[:2])
        when = datetime.fromtimestamp(t0, LOCAL_TZ)
        print(f"  {when:%a %H:%M}  LAeq {leq:5.1f}  L90 {sorted(r['l90_db'] for r in rs)[30]:5.1f}  "
              f"Lmax {max(r['lmax_db'] for r in rs):5.1f}  {top}")


def run_processes(args: argparse.Namespace) -> None:
    """Start one child per shard and serve their combined status on --status-port."""
    import urllib.request

    n = args.processes
    base = [sys.executable, str(Path(__file__).resolve())]
    passthrough = []
    for k, v in vars(args).items():
        if k in ("processes", "shard", "status_port", "preview") or v in (None, False):
            continue
        flag = "--" + k.replace("_", "-")
        passthrough += [flag] if v is True else [flag, str(v)]
    ports = [args.status_port + 1 + i if args.status_port else 0 for i in range(n)]
    if args.backfill_hours > 0:
        # Live shards start right away; N more processes compute the history up to this moment,
        # upload it and exit. They hear the same city: cars, trams and flights come from the World.
        k = passthrough.index("--backfill-hours")
        live = passthrough[:k] + passthrough[k + 2:]
        end = str(time.time())
        children = [subprocess.Popen(base + live + ["--shard", f"{i}/{n}", "--status-port", str(ports[i])])
                    for i in range(n)]
        children += [subprocess.Popen(base + passthrough + ["--shard", f"{i}/{n}", "--status-port", "0",
                                                            "--backfill-only", "--backfill-end", end])
                     for i in range(n)]
    else:
        children = [subprocess.Popen(base + passthrough + ["--shard", f"{i}/{n}", "--status-port", str(ports[i])])
                    for i in range(n)]

    def gather(path: str) -> List[Any]:
        out = []
        for p in ports:
            try:
                with urllib.request.urlopen(f"http://localhost:{p}{path}", timeout=3) as r:
                    out.append(json.loads(r.read()))
            except Exception:
                pass
        return out

    class Combined(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: Any) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, STATUS_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/devices":
                devs = [d for part in gather("/api/devices") for d in part["devices"]]
                self._send(200, json.dumps({"devices": sorted(devs, key=lambda d: d["id"])}).encode())
            elif path == "/api/status":
                parts = gather("/api/status")
                tot: Dict[str, Any] = {"server": parts[0]["server"] if parts else None, "shards": len(parts),
                                       "states": {}}
                for p in parts:
                    for k in ("devices", "sent", "failed", "pending", "dropped"):
                        tot[k] = tot.get(k, 0) + p[k]
                    for k, v in p["states"].items():
                        tot["states"][k] = tot["states"].get(k, 0) + v
                    tot["scheduler_lag_s"] = max(tot.get("scheduler_lag_s", 0), p["scheduler_lag_s"])
                self._send(200, json.dumps(tot).encode())
            else:
                self._send(404, b'{"ok": false}')

        def do_POST(self) -> None:
            res = []
            for p in ports:
                try:
                    req = urllib.request.Request(f"http://localhost:{p}{self.path}", data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=3) as r:
                        res.append(json.loads(r.read()))
                except Exception:
                    pass
            n_dev = sum(r.get("devices", 0) for r in res)
            self._send(200 if n_dev else 404, json.dumps({"ok": bool(n_dev), "devices": n_dev}).encode())

    if args.status_port:
        ThreadingHTTPServer.allow_reuse_address = True
        srv = ThreadingHTTPServer(("0.0.0.0", args.status_port), Combined)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[Fleet] {n} processes, combined status at http://localhost:{args.status_port}", flush=True)
    try:
        for c in children:
            c.wait()
    except KeyboardInterrupt:
        for c in children:
            try:
                c.wait(timeout=5)
            except subprocess.TimeoutExpired:
                c.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock Night-Noise Boxes uploading to the map server")
    ap.add_argument("--server", default="http://localhost:8080", help="map server base URL (or full .../api/readings)")
    ap.add_argument("--devices", default=str(HERE / "devices.json"), help="device list (JSON)")
    ap.add_argument("--generate", type=int, default=0, metavar="N", help="add N generated boxes around the listed ones")
    ap.add_argument("--limit", type=int, default=0, metavar="N", help="only run the first N boxes")
    ap.add_argument("--shard", default="0/1", metavar="I/N", help="run every N-th box starting at I (one process per shard)")
    ap.add_argument("--analysis-seconds", type=float, default=10.0, help="one upload per this many seconds (box: ANALYSIS_SECONDS)")
    ap.add_argument("--reading-seconds", type=float, default=10.0, help="length of each reading (box: READING_SECONDS)")
    ap.add_argument("--backfill-hours", type=float, default=0.0, help="first send this much history (max 24)")
    ap.add_argument("--backfill-only", action="store_true",
                    help="only send the history up to --backfill-end, then exit (used by --processes)")
    ap.add_argument("--backfill-end", type=float, default=0.0, metavar="EPOCH", help=argparse.SUPPRESS)
    ap.add_argument("--senders", type=int, default=8, help="parallel upload threads")
    ap.add_argument("--status-port", type=int, default=8090, help="fleet status page/API port (0 = off)")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--quiet", action="store_true", help="don't log individual outages and upload failures")
    ap.add_argument("--preview", metavar="ID", help="print a day of simulated hourly levels for one box and exit")
    ap.add_argument("--city", action="store_true", help="run the boxes on real buildings from city/ instead of --devices")
    ap.add_argument("--processes", type=int, default=1, metavar="N",
                    help="split the fleet over N processes (shards); status ports follow --status-port")
    args = ap.parse_args()

    if args.processes > 1:
        run_processes(args)
        return

    weather = Weather(args.seed)  # same seed in every shard: one city, one weather
    world = None
    if args.city:
        specs, world = load_city(args.seed, weather)
    else:
        specs = load_specs(Path(args.devices), args.generate, args.seed)
    if args.limit:
        specs = specs[: args.limit]
    i, n = (int(x) for x in args.shard.split("/"))
    specs = specs[i::n]
    devices = [MockDevice(s, weather, args.seed, world) for s in specs]

    if args.preview:
        dev = next((d for d in devices if d.id == args.preview), None)
        if dev is None:
            raise SystemExit(f"no device {args.preview!r}")
        preview(dev, 24)
        return

    if args.reading_seconds <= 0 or args.analysis_seconds < args.reading_seconds:
        raise SystemExit("need 0 < --reading-seconds <= --analysis-seconds")
    sender = Sender(args.server)
    fleet = Fleet(devices, sender, args.analysis_seconds, args.reading_seconds, args.senders, not args.quiet)
    if args.backfill_only:
        print(f"[Fleet] backfill: {args.backfill_hours:g} h of history for {len(devices)} boxes "
              f"(shard {args.shard}) -> {sender.url}", flush=True)
        try:
            fleet.backfill(min(24.0, args.backfill_hours), end=args.backfill_end or time.time())
            fleet.drain()
        except KeyboardInterrupt:
            pass
        fleet.stop()
        t = fleet.totals()
        print(f"[Fleet] backfill shard {args.shard} done: {t['sent']} sent, {t['pending']} unsent", flush=True)
        return
    rate = len(devices) / args.analysis_seconds
    print(f"[Fleet] {len(devices)} mock boxes (shard {args.shard}) -> {sender.url}, "
          f"one upload each per {args.analysis_seconds:g}s (~{rate:.1f} uploads/s)", flush=True)

    if args.status_port:
        ThreadingHTTPServer.allow_reuse_address = True
        srv = ThreadingHTTPServer(("0.0.0.0", args.status_port), make_status_handler(fleet))
        threading.Thread(target=srv.serve_forever, name="status", daemon=True).start()
        print(f"[Fleet] status at http://localhost:{args.status_port}", flush=True)

    if args.backfill_hours > 0:
        fleet.backfill(min(24.0, args.backfill_hours))
    try:
        fleet.run()
    except KeyboardInterrupt:
        pass
    finally:
        fleet.stop()
        t = fleet.totals()
        print(f"\n[Fleet] stopped: {t['sent']} sent, {t['pending']} unsent, {t['failed']} failed attempts")


if __name__ == "__main__":
    main()
