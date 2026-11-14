# Night-Noise Box on Arduino UNO Q

We built a small bedside and window-sill box on the Arduino UNO Q that listens for night-time noise, classifies each noise event on the device (`traffic`, `construction`, `music`, `voices`, `background`), and plots Night and Area heatmaps on a local web page hosted by the board. It never stores, logs, or sends audio—it keeps only label, loudness, start time, duration, and button/tamper flags. The only outbound traffic is an optional JSON upload of those scalar event records to your own Noise Map server (`UPLOAD_URL` in `python/config.py`; set it to `""` to stay fully offline).

## How It Works

- **`python/config.py` (Single Config File):** Holds every tunable default in one place: `AUDIO_SOURCE` (`"fake"` by default), `CALIBRATION_OFFSET_DB`, `GRID_CELL`, `RETENTION_DAYS`, `DEFAULT_PROFILE` (`"night"`), and the two runtime threshold profiles (`"night"`: `52 / 45 dB`; `"demo"`: `70 / 62 dB`).
- **`python/audio.py` (Window Loudness & Labels):** Produces `(level_db, label, confidence)` plus uncalibrated `raw_db` (`dBFS`) ~8 times per second (`125 ms` window). Audio PCM samples exist only in one fixed-size `array('h')` buffer in RAM that is overwritten in place every window. `LiveAudioSource` exposes a single `read_window()` method marked `TODO(BOARD-CHECK)` for physical UNO Q microphone + Edge Impulse verification.
- **`sketch/sketch.ino` (STM32 Microcontroller + Modulinos on `Wire1`):** Receives `level(db)` (~8 Hz), `label(name, confidence)` (on change), and `set_thresholds(start_db, end_db, start_hold_ms, end_hold_ms)` over `Bridge`. It drives an 8-LED loudness bar on `ModulinoPixels` (or an alternating outer-LED `"no signal"` pattern if no `level` arrives for 2 seconds), decides `event_start()` and `event_end(duration_ms)` with hysteresis, emits `woke_me()` from `ModulinoButtons`, polls `ModulinoMovement` at ~20 Hz to emit `tamper()`, and sends a 1 Hz `heartbeat()`.
- **`python/store.py` (SQLite Store & CSV Export):** Uses Python's standard-library `sqlite3` (no storage brick) to store scalar event records, prune rows older than `RETENTION_DAYS`, attach `< 10 minute` `woke_me()` presses, pre-aggregate Night and Area heatmap buckets, and export event records as CSV.
- **`python/main.py` (Linux Coordinator):** Connects `audio.py`, `Bridge`, `store.py`, and the local web UI (`WebUI` brick on UNO Q, or local HTTP/SSE server on a laptop). Timestamps `event_start`, computes `avg_level`, `peak_level`, and longest-active-time `label` + average `confidence`, tracks the 60-second `tamper` latch, manages 3-second rolling calibration persistence, and pushes `diagnostics` once per second.
- **`assets/index.html` (Offline Web Page):** Single plain HTML/CSS/JS frontend (with local `assets/area-map.svg`) readable from 2 metres away. Shows live calibrated `dB`, raw `dBFS`, active label, threshold profile switcher, phone-meter calibration control, Night Heatmap, Area Heatmap, collapsible Diagnostics panel, and CSV export.

## Repository Layout

- **`night-noise-box/`** — everything that goes on the UNO Q, as a self-contained Arduino App Lab app: `app.yaml`, `sketch/` (STM32 firmware + pinned `sketch.yaml` build profile), `python/` (Linux side), `assets/` (local web page). See [Quick Start](#quick-start-run-the-server-and-flash-the-board).
- **`server/`** — the Noise Map server (`server.py`); runs on your computer, not the board. Stores uploaded events in `noise_map.sqlite3` and serves `server/static/`.
- **`server/static/`** — the noise world map (MapLibre; live box readings anywhere, plus an OSM street propagation model for Otaniemi; no build step). See [`server/static/README.md`](server/static/README.md).

## Quick Start: Run the Server and Flash the Board

Everything is Python 3 standard library on your computer — no `pip install` or `npm` needed.

### 1. Start the Noise Map server (on your computer)

```bash
python3 server/server.py              # map at http://localhost:8080, uploads at /api/events
```

- Listens on `0.0.0.0:8080` so boxes on the same LAN can reach it. Options: `--port <N>`, `--db <path>`, `--no-seed` (start empty), `--reseed` (regenerate simulated test events), `--center lat,lon` (where to place them).
- Find your computer's LAN IP (`hostname -I` / `ip -4 addr`) — the board needs it in the next step.
- The map at `http://localhost:8080` is a world map of the boxes' live readings from `/api/readings`; each box appears at its `DEVICE_LAT`/`DEVICE_LON`. Around Otaniemi it also shows the street-level noise estimate. Open `http://localhost:8080/?demo` for the built-in demo feed.

### 2. Configure the box (`night-noise-box/python/config.py`)

App Lab starts `python/main.py` without CLI arguments, so on the board these come from `config.py`:

```python
UPLOAD_URL   = "http://<your-computer-lan-ip>:8080/api/events"   # "" = fully offline
DEVICE_ID    = "box-01"                                           # unique per box
DEVICE_LAT   = 60.1841                                            # where this box sits
DEVICE_LON   = 24.8301
AUDIO_SOURCE = "fake"                                             # keep "fake" until the live mic is verified
```

### 3. Wire the Modulinos

Connect `ModulinoPixels`, `ModulinoButtons`, and `ModulinoMovement` (plus optional unused `ModulinoKnob`) to the UNO Q **Qwiic (`Wire1`)** port.

### 4. Copy the app to the UNO Q and flash it

```bash
night-noise-box/deploy.sh <board-ip-or-hostname>      # optional 2nd arg: app folder name (default night-noise-box)
```

This copies the app over SSH as `arduino@<board>` into `/home/arduino/ArduinoApps/night-noise-box`, skipping local `*.sqlite3`, calibration files and `__pycache__`. Then in **Arduino App Lab**:

1. Open the **Night-Noise Box** app (or open the `night-noise-box/` folder directly instead of using `deploy.sh`).
2. Press **Run**. App Lab compiles `sketch/sketch.ino` with the pinned `sketch/sketch.yaml` profile (`arduino:zephyr:unoq`, library versions pinned there), flashes it to the STM32, and starts `python/main.py` on the Linux side. The `web_ui` brick and port `7000` are already declared in `app.yaml` — nothing to enable by hand.
3. Open `http://<uno-q-ip>:7000` from a device on the same network for the box's local page.

After any code or config change, re-run `deploy.sh` and press **Run** again.

> `AUDIO_SOURCE = "live"` / `--source live` intentionally exits with a message pointing at `TODO(BOARD-CHECK)` in `python/audio.py` until the live microphone + Edge Impulse reader is verified (checklist step 6 below).

### Periodic analysis mode (default: `RUN_MODE = "analysis"`)

`python/main.py` now starts `python/analysis.py` by default. It listens to the USB camera mic
(`arecord` pipe, auto-detected USB capture card, nothing written to disk). Every
`ANALYSIS_SECONDS` (10 s for now) it POSTs one summary to `READINGS_URL`
(`/api/readings`) in the map's `v: 1` format: `laeq_db`, `lmax_db`, `l90_db`, `valid_s`,
`sources` (Edge Impulse class shares) and `flags`. Set `RUN_MODE = "events"` to get the original
MCU event pipeline and local page back.

```bash
python3 night-noise-box/python/mic.py --list                     # on the board: which capture device "auto" picks
python3 night-noise-box/python/mic.py --seconds 3                # live dBFS check
python3 night-noise-box/python/analysis.py --once                # one 10 s analysis, uploaded
python3 night-noise-box/python/analysis.py --mic synthetic --once --readings-url ''   # laptop, no mic
python3 night-noise-box/python/analysis.py --self-test
```

Until the Edge Impulse model is dropped into `night-noise-box/models/` (see
`models/README.md`), loudness is measured for real, `sources` is empty and readings are flagged
`no_model`. The server stores payloads unchanged and serves them at
`GET /api/readings?since=…`; the map reads them by default (`?demo` shows the demo feed instead).
Each box appears on the map at the `lat`/`lon` it sends (`DEVICE_LAT`/`DEVICE_LON`), with no
registration needed. The valid-audio minimum (`minValidSeconds`, per 60 s) scales with each reading's
`duration_s`, so 10 s readings work as-is.

### Run without a board (laptop dev mode)

```bash
python3 server/server.py                                    # terminal 1
python3 night-noise-box/python/main.py --source fake        # terminal 2 → http://localhost:7000
```

`main.py` options: `--port <N>`, `--sim-buttons` (simulate "woke me" presses), `--upload-url <url>` (`''` disables upload), `--db <path>`.

Self-tests:

```bash
python3 night-noise-box/python/audio.py --self-test
python3 night-noise-box/python/sim_imu_replay.py
python3 night-noise-box/python/store.py --self-test
python3 night-noise-box/python/main.py --self-test
```

## Noise Map Server Details

The STM32 has no network, so it reports over `Bridge`; `python/uploader.py` on the Linux side POSTs each finished event on a background thread with retry/backoff, so a down server never stalls the audio loop.

- `POST /api/events` — `{"device_id", "lat", "lon", "events": [<event record>, ...]}`; records are upserted on `(device_id, id)` so a later "woke me" press updates the same event.
- `GET /api/events?hours=N` — `{"events": [...], "devices": [...]}`.
- Everything else is served from `server/static/` (the street-level map).

## Dependencies (One-Line Reasons)

- **Python 3 standard library (`array`, `csv`, `json`, `math`, `sqlite3`, `http.server`, `threading`)** — Handles fixed-buffer RMS dBFS math, local SQLite storage, CSV export, the map server and laptop dev serving with zero external packages.
- **`arduino-app-utils` & `arduino-app-bricks` (pre-installed in UNO Q App Lab)** — Provides `Bridge` RPC between Linux and the STM32 sketch and hosts `assets/index.html` over local Socket.IO via `WebUI`.
- **MapLibre GL (CDN) + OpenFreeMap tiles** — Basemap for the street-level map; no API key.

## How to Switch Profiles & Calibrate

- **Switch Threshold Profiles (No Reflash):** On the web page, click **Night** (`Start 52 dB · End 45 dB`) for a quiet bedroom or **Demo** (`Start 70 dB · End 62 dB`) for a noisy hall. Linux immediately sends `set_thresholds(...)` to the STM32 over `Bridge`.
- **Calibrate Against a Phone Sound Meter:** Hold a phone sound-meter app right beside the USB microphone in steady room sound for at least 3 seconds, read its `dB` figure, type it into **Loudness Calibration** on the page, and click **Calibrate**. Linux computes `CALIBRATION_OFFSET_DB = phone_reading - mean_raw_db_last_3s`, applies it immediately, and saves `<db>.calibration.json` next to `night_noise.sqlite3` so the offset persists across restarts.

## Ordered Physical Board-Testing Checklist

Execute these checks in order on the physical Arduino UNO Q:

1. **Verify UNO Q System Clock First:** Open **Diagnostics & Board Health** on `http://<uno-q-ip>:7000` with `AUDIO_SOURCE = "fake"` and confirm **Board Time** and **Browser Time** agree within 2 minutes (no clock-mismatch banner) so SQLite event timestamps land in the right night hour.
2. **Confirm STM32 Bridge Registration & 1 Hz Heartbeat:** Check App Lab compile logs for `void onLabel(String name, float confidence)` (`TODO(BOARD-CHECK)` in `sketch/sketch.ino` if Zephyr requires `std::string`), and verify Diagnostics shows `Microcontroller: Responding (OK)` and `ModulinoPixels` renders the 8-LED bar instead of the alternating outer-LED `"no signal"` pattern.
3. **Test Runtime Profile Switching (`Night` <-> `Demo`):** Click **Demo** on the page while running fake audio (`peak ~65 dB`); confirm no new events open because `65 dB < 70 dB`, then switch back to **Night** (`52 dB`) and confirm fake traffic/voices/music events open and close cleanly.
4. **Verify Physical `ModulinoButtons` (`woke_me`) & `ModulinoMovement` (`tamper`):** Press a button on `ModulinoButtons` within 10 minutes of a fake event and confirm the Night Heatmap outlines that cell with `WOKE ×1`; gently nudge or lift the box to confirm `ModulinoMovement` (~20 Hz) marks the next event within 60 seconds with `TAMPER`.
5. **Verify Watchdog Recovery:** Stop `python/main.py` while leaving the STM32 powered; confirm `ModulinoPixels` switches to the alternating endpoint `"no signal"` pattern after 2 seconds and resumes the loudness bar as soon as `python/main.py` restarts.
6. **Wire & Benchmark Live USB Microphone + Edge Impulse Model (`TODO(BOARD-CHECK)` in `python/audio.py`):** Test on the UNO Q Debian terminal which of the three candidates in `LiveAudioSource.read_window()` delivers ~8 Hz `(level_db, label, confidence)` cleanly without touching disk:
   - *(a)* `AudioClassification` brick for labels + shared `Microphone` stream into `_pcm_buf`,
   - *(b)* single `Microphone` loop overwriting `_pcm_buf` + Edge Impulse Linux `.eim` runner, or
   - *(c)* `AudioClassification` brick for labels + `mcu_level(db)` from an analog mic on the STM32.
   Once verified, set `AUDIO_SOURCE = "live"` in `python/config.py`.
7. **Calibrate Live Microphone & Validate CSV Export:** Hold a phone sound meter next to the USB microphone, run **Calibrate** from the page, restart the app in App Lab to confirm the saved offset reloads from `night_noise.calibration.json`, and click **Export CSV** to confirm only the 10 scalar event columns download.
