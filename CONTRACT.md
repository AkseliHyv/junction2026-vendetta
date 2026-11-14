# CONTRACT.md — Night-Noise Box Message, Storage & Privacy Specification

**Scope:** Every runtime module (`python/config.py`, `sketch/sketch.ino`, `python/audio.py`, `python/store.py`, `python/main.py`, `assets/index.html`) implements this exact contract. No message name, argument order, field name, or unit may change without updating this document first.

---

## 1. System, Configuration & Privacy Invariants

1. **Zero Audio Persistence or Transmission:**
   - The USB microphone is connected to the UNO Q Linux side only; the STM32 microcontroller never receives audio samples.
   - Inside `python/audio.py`, PCM samples exist only in a fixed-size in-memory buffer (`array('h')`) that is overwritten in place on every window.
   - Raw or encoded audio is **never** written to disk, **never** sent over the Bridge or network, **never** included in CSV exports, and **never** printed to logs. Test audio files may exist only under `tests/` and are never read by the running application.
2. **Offline-Only Runtime:**
   - Nothing in the application calls the internet at runtime: no cloud APIs, no map tiles, no external fonts, and no CDN scripts. `assets/index.html` is the sole frontend and loads all scripts, styles, and the static area background image (`assets/area-map.svg`) locally from `assets/`.
3. **Single Configuration File (`python/config.py`):**
   - Holds every tunable default in one place:
     - `AUDIO_SOURCE`: `"fake"` | `"live"` (default `"fake"`). Overridable via `python3 python/main.py --source fake|live`.
     - `CALIBRATION_OFFSET_DB`: `float` (default `94.0` dB). Overridden at startup when `<db>.calibration.json` exists next to the SQLite database.
     - `GRID_CELL`: `str` (default `"B2"`), one configured constant for this physical UNO Q box on the area heatmap.
     - `RETENTION_DAYS`: `int` (default `14`).
     - `DEFAULT_PROFILE`: `"night"` (`"night"` | `"demo"`).
     - `PROFILES`:
       - `"night"`: `{"start_db": 52.0, "end_db": 45.0, "start_hold_ms": 375, "end_hold_ms": 625}`
       - `"demo"`: `{"start_db": 70.0, "end_db": 62.0, "start_hold_ms": 375, "end_hold_ms": 625}` (for a loud exhibition hall)
4. **Allowed Noise Labels:**
   - Edge Impulse classifier output is normalized to one of five canonical acoustic classes:
     `"traffic"`, `"construction"`, `"music"`, `"voices"`, `"background"`.
   - Stored event records may additionally use `"unknown"` **only** when `woke_me()` creates a standalone record because no event is open and no event ended within the last 10 minutes.
5. **Loudness Calibration (`dBFS` -> `dB`):**
   - `python/audio.py` computes uncalibrated window loudness `raw_db` in `dBFS` (`<= 0.0`) from RMS amplitude and adds `CALIBRATION_OFFSET_DB` to obtain calibrated loudness `level_db` (`0.0 .. 120.0 dB`).
   - When calibrated from the web page against a handheld reference sound meter reading `phone_db`, Linux sets `CALIBRATION_OFFSET_DB = round(phone_db - mean_raw_db_last_3s, 1)` (averaging `raw_db` over the preceding 3 seconds) and persists `{"offset_db": float, "calibrated_at": "YYYY-MM-DDTHH:MM:SS"}` to `<db>.calibration.json` next to the SQLite database so it survives restarts.
6. **Hardware Roles on Qwiic (`Wire1`, Microcontroller Side):**
   - `ModulinoPixels`: Live 8-LED loudness bar driven by `level(db)`, plus a distinct `"no signal"` pattern if the 2-second watchdog trips.
   - `ModulinoButtons`: Rising-edge press emits `woke_me()`.
   - `ModulinoMovement`: Polled at **~20 Hz (`50 ms`)** to detect box movement and emit `tamper()`.
   - `ModulinoKnob`: Existing setup/polling code in `sketch/sketch.ino` is left in place unused (ignored by Linux; not built upon). `ModulinoThermo` is not added.

---

## 2. Division of Event Responsibility & Timing Rules

1. **MCU Decides Event Boundaries Only (Runtime-Tunable Thresholds):**
   - The microcontroller decides **only** `event_start` and `event_end`.
   - `sketch/sketch.ino` defines named default constants at the top of the file (`START_THRESHOLD_DB = 52.0f`, `END_THRESHOLD_DB = 45.0f`, `START_HOLD_MS = 375UL`, `END_HOLD_MS = 625UL`, `NO_SIGNAL_TIMEOUT_MS = 2000UL`) in the same `dB` (and `ms`) units as `level(db)`, and updates its active runtime thresholds whenever `set_thresholds(start_db, end_db, start_hold_ms, end_hold_ms)` arrives from Linux (sent on startup and on profile switch).
   - **Linux** timestamps `start_time` upon receipt of `event_start` and computes `avg_level` and `peak_level` across the calibrated `level(db)` windows observed while the event is open.
2. **Event Label & Confidence Rule:**
   - An event's `label` is the label that was **active for the longest total time** during the event (`event_start` -> `event_end`).
   - Its `confidence` is the **time-weighted average confidence** of that winning label over the time it was active during the event.
3. **`woke_me()` Attachment Rule:**
   - If an event is currently open, `woke_me()` sets `woke_me = True` on that open event.
   - Else if the latest completed event ended **less than 10 minutes (`600 s`) ago**, `woke_me()` sets `woke_me = True` on that stored record in SQLite and re-emits `event(record)` and `heatmap(data)`.
   - Else Linux creates and stores a standalone event record at the current timestamp with `duration_ms = 0`, `label = "unknown"`, `confidence = 0.0`, `avg_level` = latest `level(db)`, `peak_level` = latest `level(db)`, `woke_me = True`, `tamper` = active tamper latch, and `grid_cell = GRID_CELL`, then emits `event(record)` and `heatmap(data)`.
4. **`tamper()` Attachment Rule:**
   - `tamper()` immediately sets `tamper = True` on the currently open event (if any) **and** starts/extends a **60-second latch** (`TAMPER_LATCH_SEC = 60.0`) so **any event starting within 60 seconds after `tamper()`** also receives `tamper = True`.
5. **MCU 2-Second Signal Watchdog & 1-Second Heartbeat:**
   - If the microcontroller receives no `level(db)` message from Linux for **2 seconds (`2000 ms`)**, it immediately sends `event_end(duration_ms)` if an event is open, resets its threshold state, and renders a distinct `"no signal"` pattern on `ModulinoPixels` until `level(db)` resumes.
   - The microcontroller emits `heartbeat()` once every **1 second (`1000 ms`)** so Linux can report MCU responsiveness (`mcu_responding = True` when a heartbeat was received within the last `3.0 s`).

---

## 3. Channel A: Linux <-> Microcontroller (`arduino.app_utils.Bridge` / `Arduino_RouterBridge`)

Transport: Asynchronous notifications via `Bridge.notify(name, ...)` and registered handlers via `Bridge.provide(name, handler)`.

### A1. Linux -> Microcontroller (`Bridge.notify` in Python, `Bridge.provide` in C++)

| Message Name | Direction | Positional Args | Types & Units | Rate | Semantics |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `level` | Linux -> MCU | `(db)` | `float` in `0.0 .. 120.0` (calibrated `dB`) | ~`8 Hz` (`~125 ms` window) | Resets the 2 s MCU watchdog, updates the `ModulinoPixels` light bar, and feeds the MCU hysteresis detector (`activeStartDb` / `activeEndDb`). |
| `label` | Linux -> MCU | `(name, confidence)` | `name: str` (`"traffic" \| "construction" \| "music" \| "voices" \| "background"`), `confidence: float` (`0.0 .. 1.0`) | Event-driven **only when `name` changes** | Informs the MCU when the active acoustic label changes (never carries audio). |
| `set_thresholds` | Linux -> MCU | `(start_db, end_db, start_hold_ms, end_hold_ms)` | `start_db: float` (`dB`), `end_db: float` (`dB`, `< start_db`), `start_hold_ms: int` (`ms`), `end_hold_ms: int` (`ms`) | Sent on Linux startup and whenever the active profile (`"night"` / `"demo"`) changes | Updates runtime hysteresis thresholds and hold durations on the MCU without reflashing. |

### A2. Microcontroller -> Linux (`Bridge.notify` in C++, `Bridge.provide` in Python)

| Message Name | Direction | Positional Args | Types & Units | Rate | Semantics |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `event_start` | MCU -> Linux | `()` | None | Event-driven | Fired when `level(db) >= activeStartDb` continuously for `activeStartHoldMs` while no event is open. Linux timestamps `start_time` on receipt and begins accumulating levels and label durations. |
| `event_end` | MCU -> Linux | `(duration_ms)` | `duration_ms: int` (`ms >= 0`) | Event-driven | Fired when an open event's `level(db) <= activeEndDb` continuously for `activeEndHoldMs`, **or** when the 2-second `"no signal"` watchdog trips while an event is open. Linux finalizes and stores the event record. |
| `woke_me` | MCU -> Linux | `()` | None | Event-driven on button press, debounced (`>= 200 ms`) | Applies the Section 2.3 `woke_me` rule (open event -> ended `< 10 min` ago -> standalone `"unknown"` record). |
| `tamper` | MCU -> Linux | `()` | None | Event-driven from ~20 Hz IMU poll, debounced (`>= 1000 ms`) | Applies the Section 2.4 `tamper` rule (marks open event and any event starting within the next `60 s`). |
| `heartbeat` | MCU -> Linux | `()` | None | `1 Hz` (`1000 ms`) | Periodic liveness signal from the MCU so Linux diagnostics can report whether the microcontroller is responding. |
| `mcu_level` | MCU -> Linux | `(db)` | `float` in `0.0 .. 120.0` (calibrated `dB`) | Optional (~`8 Hz`), **unused by default** | Optional fallback path if loudness is measured from an analog microphone on the MCU while Linux runs only the `AudioClassification` brick for labels. |

*(Legacy unused message: `knob(value: int)` remains in `sketch/sketch.ino`; `python/main.py` registers a no-op receiver and does not act on it.)*

---

## 4. Event Record Schema (`python/store.py` SQLite, Wire & CSV Export Format)

Persisted in local SQLite (`python/store.py` via standard-library `sqlite3`, no storage brick) and pruned automatically when records are older than `RETENTION_DAYS` (default `14` days). CSV exports contain **only** these 10 scalar columns in this order.

| Field Name | Python / JSON Type | SQLite Column Type | Units / Allowed Values | Description |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `int` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `>= 1` | Unique local record identifier. |
| `start_time` | `str` | `TEXT NOT NULL` | ISO-8601 local timestamp (`"YYYY-MM-DDTHH:MM:SS"`) | Timestamp recorded by Linux upon receiving `event_start` (or standalone `woke_me`). |
| `duration_ms` | `int` | `INTEGER NOT NULL` | Milliseconds (`>= 0`) | Duration reported by `event_end(duration_ms)` (`0` for standalone `"unknown"` `woke_me` records). |
| `label` | `str` | `TEXT NOT NULL` | `"traffic" \| "construction" \| "music" \| "voices" \| "background" \| "unknown"` | Label active for the longest total time during the event (`"unknown"` only for standalone `woke_me`). |
| `confidence` | `float` | `REAL NOT NULL` | `0.0 .. 1.0` | Average confidence of the winning `label` over the time it was active during the event (`0.0` for `"unknown"`). |
| `avg_level` | `float` | `REAL NOT NULL` | `dB` (`round(..., 1)`) | Mean of `level(db)` samples observed by Linux during the event. |
| `peak_level` | `float` | `REAL NOT NULL` | `dB` (`round(..., 1)`) | Maximum `level(db)` sample observed by Linux during the event. |
| `woke_me` | `bool` | `INTEGER NOT NULL` (`0` or `1`) | `true` / `false` | `true` if marked via `woke_me()`. |
| `tamper` | `bool` | `INTEGER NOT NULL` (`0` or `1`) | `true` / `false` | `true` if `tamper()` fired during the event or within `60 s` prior to `event_start`. |
| `grid_cell` | `str` | `TEXT NOT NULL` | Configured constant `GRID_CELL` (e.g. `"B2"`) | Single configured area-grid cell identifier for this box. |

---

## 5. Channel B: Linux -> Browser (`WebUI.send_message(event, payload)`)

Transport: Arduino App Lab `WebUI` brick over Socket.IO (`socket.on(event, payload)`), with local HTTP/SSE fallback (`/api/events`, `/api/cmd`, `/export.csv`) when running `python/main.py` on a laptop without App Lab.

| Event Name | Direction | JSON Payload Structure | Rate | Description |
| :--- | :--- | :--- | :--- | :--- |
| `live` | Linux -> Browser | `{"level": float, "raw_db": float, "label": str}` | ~`8 Hz` | Current calibrated loudness (`level` in `dB`), uncalibrated raw loudness (`raw_db` in `dBFS`), and active acoustic `label`. |
| `event` | Linux -> Browser | `record` dict matching **Section 4** (`id`, `start_time`, `duration_ms`, `label`, `confidence`, `avg_level`, `peak_level`, `woke_me`, `tamper`, `grid_cell`) | Event-driven on `event_end`, standalone `woke_me`, or `woke_me` update | Newly completed or updated noise event record. |
| `heatmap` | Linux -> Browser | `{"night": list[NightBucket], "area": list[AreaBucket], "hours": list[int], "labels": list[str], "box_cell": str, "recent_events": list[EventRecord]}` | On browser connect + after every `event` insert/update/prune | Pre-aggregated buckets for both heatmaps (plus up to 20 recent event records for initial table hydration) on `assets/index.html`. |
| `diagnostics` | Linux -> Browser | `{"audio_source": str, "active_profile": str, "profiles": dict, "calibration_offset_db": float, "calibrated_at": str \| null, "sec_since_last_level": float \| null, "mcu_responding": bool, "events_stored": int, "db_size_bytes": int, "board_time_iso": str, "board_epoch_ms": int}` | `1 Hz` + on connect, profile change, or calibration update | Telemetry for the collapsible Diagnostics panel, profile controls, calibration status, and board-vs-browser clock skew check (`> 120 s` warning). |
| `csv_export` | Linux -> Browser | `{"filename": str, "csv": str}` | On `export_csv` request | Complete CSV export of stored scalar event records (`Section 4` columns only; never audio). |

### `heatmap(data)` Pre-Aggregated Bucket Shapes

1. **Night View Buckets (`data.night`)** — Hours of the night across (`data.hours`, e.g. `[22, 23, 0, 1, 2, 3, 4, 5, 6]`), noise labels down (`data.labels`):
   ```json
   {
     "hour": 23,
     "label": "traffic",
     "count": 4,
     "avg_level": 61.2,
     "woke_count": 1
   }
   ```
   - Colour encodes how loud (`avg_level`) and how often (`count`).
   - Cells with `woke_count > 0` are visibly outlined to mark `"woke me"` events.

2. **Area View Buckets (`data.area`)** — Plain grid drawn over a static background image (`assets/area-map.svg`) served locally from `assets/`:
   ```json
   [
     { "cell": "B2", "count": 7, "avg_level": 58.4, "simulated": false },
     { "cell": "A3", "count": 5, "avg_level": 54.0, "simulated": true }
   ]
   ```
   - Exactly one cell (`cell == GRID_CELL`, `"simulated": false`) reflects real SQLite records from this box.
   - Sample neighbour cells carry `"simulated": true` and **must** be visibly labelled `"simulated"` on the page.

---

## 6. Channel C: Browser -> Linux (`socket.emit(event, payload)`)

| Event Name | Direction | JSON Payload | Description |
| :--- | :--- | :--- | :--- |
| `request_state` | Browser -> Linux | `{}` | Sent by `assets/index.html` on load/reconnect so Linux immediately pushes current `live`, `heatmap`, and `diagnostics`. |
| `set_profile` | Browser -> Linux | `{"profile": "night" \| "demo"}` | Switches active threshold profile, sends `set_thresholds(...)` to the MCU, and broadcasts updated `diagnostics`. |
| `calibrate` | Browser -> Linux | `{"phone_db": float}` | Sets `CALIBRATION_OFFSET_DB = phone_db - mean_raw_db_last_3s`, persists `<db>.calibration.json` next to SQLite, updates live calibration immediately, and broadcasts `live` + `diagnostics`. |
| `export_csv` | Browser -> Linux | `{}` | Triggers `csv_export` carrying all stored event records in CSV format (never audio). |
