# CONTRACT.md — Shared Message & Timing Specification

**Owner:** Agent 0 (Architecture & Protocol Lead)  
**Rule:** Every module (`sketch/sketch.ino`, `python/pose.py`, `python/exercises.py`, `python/main.py`, `assets/synth.js`, `assets/index.html`) implements this exact contract. No message name, argument order, field name, or unit may change without updating this document first.

---

## 1. Timing & Privacy Invariants

1. **Wrist-Driven Onsets Only:** Note onsets (`note_on`) are triggered exclusively by microcontroller `strike` events (or explicit simulated strikes on a laptop), never by camera frame arrivals.
2. **Pose Sets Pitch & Harmony Only:** Camera pose estimation updates cached `left` and `right` normalized arm heights (`0.0` = hand resting at lap, `1.0` = hand raised comfortably overhead while seated). When a `strike` arrives, Linux maps the latest cached arm levels to notes in the C major pentatonic scale (`C4, D4, E4, G4, A4, C5, D5, E5`) in `< 1 ms`.
3. **Latency Budget:** Target `< 100 ms` end-to-end from wrist strike detection to Web Audio scheduling in the browser. Every Linux -> Browser message carries `ts_ms` (Unix epoch milliseconds, `int(time.time() * 1000)`) so end-to-end latency is logged.
4. **Strict Visual Privacy:** Camera frames exist only in volatile RAM inside `python/pose.py`. Frames are never saved to disk and never transmitted to the browser.
5. **Never-Fail Rehabilitation UX:** No message or UI state represents "failure", "error", or "wrong pose". Unreached steps wait patiently while movement continues making music.

---

## 2. Channel A: Microcontroller <-> Linux (`Arduino_RouterBridge` / `arduino.app_utils.Bridge`)

Transport: RPC / asynchronous notifications via `Bridge.notify(name, ...)` (fire-and-forget for low latency) and `Bridge.provide(name, handler)`.

### A1. Microcontroller -> Linux (`Bridge.notify` in C++, `Bridge.provide` in Python)

| Message Name | Direction | Positional Args | Types & Units | Max / Typical Rate | Semantics |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `strike` | MCU -> Linux | `(intensity)` | `float` in `0.05 .. 1.00` (dimensionless normalized jerk/acceleration peak above threshold) | Event-driven, debounced (`>= 95 ms` gap, max ~10 Hz) | Immediate wrist strike onset. Triggers `note_on` in Linux using current arm heights. |
| `tempo` | MCU -> Linux | `(bpm)` | `int` in `30 .. 220` (`beats per minute`) | Event-driven on >= 2 strikes within 2.5 s (max ~4 Hz) | Rolling median inter-strike tempo computed over last 4 strike intervals. |
| `button` | MCU -> Linux | `(button_id)` | `int` in `{0, 1, 2}` (`0` = Button A: Mode toggle, `1` = Button B: Next exercise / Start session, `2` = Button C: Record custom pose step) | Event-driven on press edge, debounced (`50 ms`) | Modulino Buttons physical press event. |
| `knob` | MCU -> Linux | `(value)` | `int` in `0 .. 100` (percent normalized rotary position) | On change (`>= 1` step delta), max 20 Hz | Modulino Knob position. Maps linearly to Instruct Mode pose tolerance (`0` = `0.06` strict, `100` = `0.28` gentle; default `50` = `0.17`). |

### A2. Linux -> Microcontroller (`Bridge.notify` in Python, `Bridge.provide` in C++)

| Message Name | Direction | Positional Args | Types & Units | Max / Typical Rate | Semantics |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `set_pixels` | Linux -> MCU | `(pattern)` | `str` enum: `"idle"`, `"free_pulse"`, `"progress_0"` .. `"progress_8"`, `"celebrate"` | Event-driven on strike, step change, or mode change (max 15 Hz) | Updates the 8 RGB LEDs on `ModulinoPixels` (never uses red; uses warm amber, teal, sky blue, and soft gold). |

---

## 3. Channel B: Linux -> Browser (`WebUI.send_message(event, payload)` over Socket.IO)

Transport: Arduino App Lab `WebUI` brick (`from arduino.app_bricks.web_ui import WebUI`), pushing JSON dictionaries over Socket.IO (`socket.on(event, payload)`). Every payload is a JSON object containing `"ts_ms"` (`int`, epoch ms).

| Event Name | Direction | JSON Payload Fields & Units | Rate | Description |
| :--- | :--- | :--- | :--- | :--- |
| `note_on` | Linux -> Browser | `{"note": int (MIDI 60..88), "velocity": float (0.05..1.0), "voice": int (0=left marimba, 1=right bell), "ts_ms": int}` | Event-driven on `strike` (1 or 2 voices per strike) | Triggers immediate Web Audio synthesis and visual wrist ripple. |
| `note_off` | Linux -> Browser | `{"voice": int (0\|1), "ts_ms": int}` | Event-driven (optional sustain release) | Gently releases sustained voice envelope if active. |
| `target_melody` | Linux -> Browser | `{"kind": str ("step_reward" \| "session_complete"), "notes": list[int] (MIDI notes), "ts_ms": int}` | Event-driven on step match or session completion | Plays the encouraging reward phrase (`step_reward`) or full celebratory melody (`session_complete`). |
| `state` | Linux -> Browser | `{"left": float (0.0..1.0), "right": float (0.0..1.0), "person_present": bool, "pose_fps": float, "fallback_wrist_only": bool, "tolerance": float (0.06..0.28), "bpm": int, "hold_progress": float (0.0..1.0), "pixels": str, "ts_ms": int}` | `15 Hz` steady tick (`66 ms`) | Live smoothed seated arm heights, sensor health, hold match ratio (`0..1`), and active Modulino Pixels state. |
| `mode` | Linux -> Browser | `{"mode": str ("free" \| "instruct"), "exercise_id": str, "exercises": list[{"id": str, "name": str, "step_count": int}], "custom_count": int, "ts_ms": int}` | On connection + on mode/exercise change | Authoritative mode and available exercise catalog owned by Linux. |
| `step` | Linux -> Browser | `{"index": int (0-based), "total": int, "left": float (0.0..1.0), "right": float (0.0..1.0), "tolerance": float (0.06..0.28), "hold_sec": float (s), "instruction": str, "exercise_name": str, "ts_ms": int}` | On session start + each step transition + tolerance change | Current target arm pose and 1-line 2m-readable instruction for the ghost stick figure. |
| `step_reached` | Linux -> Browser | `{"index": int (0-based), "total": int, "ts_ms": int}` | Event-driven when hold completes | Confirms target pose held for `hold_sec` within `tolerance`. |
| `session_done` | Linux -> Browser | `{"steps_completed": int, "total_steps": int, "duration_sec": float, "exercise_name": str, "notes_played": int, "ts_ms": int}` | Event-driven after final step completes | Session summary displayed on screen alongside celebratory melody. |

---

## 4. Channel C: Browser -> Linux (`socket.emit(event, payload)` -> `WebUI.on_message`)

Transport: Socket.IO client `socket.emit(event, payload)` caught by `web_ui.on_message(event, handler)`. The page never mutates mode or step state locally without the board's broadcast, except when running in explicit offline browser-only demo mode (`NO_BOARD_SIM`).

| Event Name | Direction | JSON Payload Fields & Units | Rate | Description |
| :--- | :--- | :--- | :--- | :--- |
| `set_mode` | Browser -> Linux | `{"mode": str ("free" \| "instruct")}` | User action | Switches between Free instrument mode and Guided Instruct mode. |
| `start_session` | Browser -> Linux | `{"exercise_id": str}` | User action | Starts (or restarts) a preset exercise or `"custom"` recorded exercise in Instruct mode. |
| `record_step` | Browser -> Linux | `{"hold_sec": float (optional, default 1.5)}` | Physio action | Captures the player's current `(left, right)` arm levels as a new step in the `"custom"` exercise. |
| `clear_custom` | Browser -> Linux | `{}` | Physio action | Clears all physio-recorded custom steps. |
| `client_latency` | Browser -> Linux | `{"latency_ms": float, "strike_ts_ms": int}` | Event-driven after `note_on` audio scheduling | Optional telemetry echo allowing `python/main.py` to print measured strike-to-sound browser latency in terminal logs. |

---

## 5. Pentatonic Scale Mapping (Shared Reference)

Arm height `h` in `[0.0, 1.0]` maps to an 8-note C Major Pentatonic scale (harmonious in every combination — zero dissonant intervals):

| Band (`0..7`) | Height Range | MIDI Note | Pitch Name | Frequency (Hz) |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `0.000 – 0.124` | `60` | `C4` | `261.63` |
| `1` | `0.125 – 0.249` | `62` | `D4` | `293.66` |
| `2` | `0.250 – 0.374` | `64` | `E4` | `329.63` |
| `3` | `0.375 – 0.499` | `67` | `G4` | `392.00` |
| `4` | `0.500 – 0.624` | `69` | `A4` | `440.00` |
| `5` | `0.625 – 0.749` | `72` | `C5` | `523.25` |
| `6` | `0.750 – 0.874` | `74` | `D5` | `587.33` |
| `7` | `0.875 – 1.000` | `76` | `E5` | `659.25` |

- **Voice `0` (Left Arm):** Warm wooden marimba fundamental (`C4..E5`).
- **Voice `1` (Right Arm):** Soft celesta/bell harmonic voice (`G4..G5` or pentatonic fifth/octave pairing when moving together). When `fallback_wrist_only` is active (camera absent or no person detected), strikes cycle gracefully through a soothing pentatonic arpeggio `[60, 64, 67, 69, 72, 69, 67, 64]` so wrist movement always makes music.
