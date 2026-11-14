# Kinesthesia — Movement-to-Music Instrument on Arduino UNO Q

We built an offline movement-to-music instrument and seated rehabilitation guide for care homes and physiotherapy clinics on the Arduino UNO Q. One seated person moves their arms to choose notes in a C major pentatonic scale while wrist motion on a Modulino IMU triggers note onsets and tempo.

## Repository Layout (Arduino App Lab)

- `CONTRACT.md` — Shared message specification across STM32, Linux Python, and Browser UI.
- `sketch/sketch.ino` — STM32 microcontroller firmware (`Arduino_RouterBridge` + `Arduino_Modulino` on `Wire1`).
- `sketch/sim_imu_replay.py` — Standalone laptop verifier for strike detection, debounce, and BPM logic.
- `python/pose.py` — USB camera seated arm-height estimator (`0.0..1.0`) with strict RAM-only privacy.
- `python/exercises.py` — Four seated rehabilitation exercises in plain Python data.
- `python/main.py` — Linux coordinator (`arduino.app_utils` + `arduino.app_bricks.web_ui`) with laptop fallback and self-test.
- `assets/synth.js` — Pre-allocated Web Audio API pentatonic marimba/bell synthesizer.
- `assets/synth-test.html` — Keyboard test page for `synth.js` with no board connected.
- `assets/index.html` — Plain HTML/JS player screen and physiotherapist panel served by the `WebUI` brick.
- `PROJECT_HUB.md` — Arduino Project Hub draft (components, wiring, how it works, how to run).

## Added Python / Board Packages (One-Line Justifications)

- `arduino-app-utils` / `arduino-app-bricks` (built into UNO Q App Lab image) — Required for `Bridge` MCU RPC and the `WebUI` Socket.IO local web server.
- `opencv-python-headless` (optional on laptop; used on UNO Q Linux for V4L2 USB capture) — Reads low-resolution `320x240` USB webcam frames into RAM without GUI bloat.
- `mediapipe` (Lite Pose `model_complexity=0` on Linux A53) — Extracts upper-body shoulder and wrist landmarks in real time while seated.

*(Note: All laptop verification scripts run with Python 3.10 standard library alone when `opencv-python-headless` and `mediapipe` are not installed.)*

## How to Run and Verify Without Hardware (Laptop)

1. **Check STM32 strike & debounce logic on recorded 200 Hz IMU trace:**
   ```bash
   python3 sketch/sim_imu_replay.py
   ```
2. **Check seated pose estimator with synthetic arm stream (or `--webcam 0`):**
   ```bash
   python3 python/pose.py --fake --steps 10
   ```
3. **Check end-to-end Linux coordinator, custom step recording, full guided session, and latency log:**
   ```bash
   python3 python/main.py --self-test
   ```
4. **Drive `assets/index.html` live from `python/main.py` on a laptop:**
   ```bash
   python3 python/main.py --laptop-server --port 7000 --fake-pose --sim-strikes
   ```
   Then open `http://localhost:7000` (or `http://localhost:7000/synth-test.html`), click **Start Audio**, and test both **Free Mode** and **Instruct Mode**.

## How to Run on Arduino UNO Q

1. Connect the Modulino chain (**Movement**, **Buttons**, **Knob**, **Pixels**) to the UNO Q Qwiic connector (`Wire1`) and strap **Modulino Movement** lightly to a soft wrist band.
2. Plug a UVC USB webcam into the UNO Q USB-C/USB-A host port facing the seated chair from ~1.8 m away.
3. Open this project folder in **Arduino App Lab**, enable the **WebUI - HTML** brick in `app.yaml` (port `7000`), and click **Run**.
4. Connect a laptop, tablet, or phone to the same local Wi-Fi network, open `http://<uno-q-local-ip>:7000`, and tap **Start Audio**.

## Numbered List of Items Still Needing Physical Board Testing

1. **Measure Cortex-A53 Pose FPS and Camera Model:** Run `python3 python/pose.py --webcam 0 --steps 100` on the UNO Q Debian terminal and record exact steady-state FPS at `320x240` (`model_complexity=0`) and camera model string for the brief's `[X fps]` / `[model/resolution]` slots.
2. **Confirm `Bridge.provide("set_pixels", ...)` String Type on Zephyr STM32:** Verify in Arduino App Lab compile logs whether `Arduino_RouterBridge` binds `void onSetPixels(String pattern)` or `void onSetPixels(std::string pattern)` on the UNO Q Zephyr core (`TODO(BOARD-CHECK)` in `sketch/sketch.ino`).
3. **Calibrate Wrist Band Strike Threshold with Real Residents:** Test `STRIKE_THRESHOLD_G = 0.42f` and `STRIKE_DEBOUNCE_MS = 95` with the physical Velcro wrist strap to confirm gentle tremor-safe strikes trigger reliably without double-firing.
4. **Verify Qwiic daisy-chain I2C cable length:** Confirm the flexible Qwiic cable from the table-mounted UNO Q (`Wire1`) to the wrist-worn `ModulinoMovement` maintains clean 100 kHz / 400 kHz I2C signaling without bus lockups during full overhead arm reach (`~80–100 cm` cable).
5. **Measure True End-to-End Wi-Fi Strike-to-Sound Latency:** Trigger physical strikes on `ModulinoMovement` while a laptop/tablet is connected over local Wi-Fi to port `7000` and confirm `python/main.py` `[Latency]` logs remain consistently `< 100 ms`.
6. **Confirm Indoor Lighting & 2-Metre Legibility in Clinic Room:** Verify `ModulinoPixels` brightness (`DEFAULT_BRIGHTNESS = 18`) and `assets/index.html` stick figure contrast from a seated position 2 metres away under warm care-home lighting.
