# Night-Noise Box on Arduino UNO Q

## What We Built
We built a small offline night-noise monitor on the Arduino UNO Q. It sits on a bedside table or window sill, listens for loud events overnight, labels what caused them (`traffic`, `construction`, `music`, `voices`, or `background`) using an on-device Edge Impulse sound model, and draws a Night Heatmap and an Area Heatmap on a local web page served by the board.

If a loud noise wakes us up, we press a button on the box without looking at a screen. If someone moves or bumps the box, the motion sensor flags `tamper` so bumped readings are not mistaken for street noise.

## Components
- 1x **Arduino UNO Q** (Linux microprocessor + STM32 microcontroller)
- 1x **USB Microphone** (plugged into the UNO Q Linux USB host port)
- 1x **Arduino Modulino Pixels** (8 RGB LEDs for the live loudness bar and `"no signal"` alert)
- 1x **Arduino Modulino Buttons** (button press marks `"woke me"`)
- 1x **Arduino Modulino Movement** (6-axis IMU polled at ~20 Hz for tamper detection)
- 3x **Qwiic I2C cables**
- 1x **Phone, tablet, or laptop** on the same local Wi-Fi network to view the page and calibrate

*(If an existing Modulino Knob is still on the Qwiic chain from earlier prototyping, the sketch leaves it harmlessly unused.)*

## Wiring
1. Plug a Qwiic cable into the **Qwiic (`Wire1`)** port on the Arduino UNO Q.
2. Daisy-chain **Modulino Pixels** -> **Modulino Buttons** -> **Modulino Movement** and mount them inside or on top of the enclosure so the buttons and LEDs are reachable from bed and the IMU sits flat with the box.
3. Plug the **USB microphone** into the UNO Q USB host port on the Linux side.

## How It Works
We split the job between the Linux side, the STM32 microcontroller, and the browser:

- **Linux (`python/config.py`, `python/audio.py`, `python/store.py`, `python/main.py`):** Reads 125 ms windows (~8 Hz) from the microphone, computes raw loudness (`dBFS`) plus our saved calibration offset (`dB`), and gets the Edge Impulse class label. It sends `level(db)` ~8 times a second and `label(name, confidence)` on changes over the `Bridge`. When the microcontroller reports `event_start()` and `event_end(duration_ms)`, Linux records the timestamp, average/peak dB, and the label active for the longest time in a local `sqlite3` database, then updates the browser heatmaps.
- **Microcontroller (`sketch/sketch.ino`):** Keeps the hysteresis detector (`Night`: start `52 dB`, end `45 dB`; `Demo`: start `70 dB`, end `62 dB`, switchable live via `set_thresholds`), updates the 8-LED bar on `ModulinoPixels`, watches `ModulinoButtons` for `woke_me()`, polls `ModulinoMovement` at ~20 Hz for `tamper()`, sends a 1 Hz `heartbeat()`, and shows a distinct outer-LED `"no signal"` pattern if `level(db)` stops for 2 seconds.
- **Local Web Page (`assets/index.html`, `assets/area-map.svg`):** Displays calibrated `dB` and raw `dBFS` in large type readable from 2 metres, switches between **Night** and **Demo** profiles without reflashing, calibrates against a phone sound-meter reading over a 3-second average, plots the Night Heatmap (hours across, labels down, `"woke me"` outlined) and Area Heatmap (one real cell for our box plus neighbour cells clearly marked `"simulated"`), shows a collapsible Diagnostics panel, and exports stored event records as CSV.

## Privacy by Design
We designed the box so raw audio cannot leak or be replayed:
- The microcontroller never sees audio; only scalar `level(db)` and `label` cross the `Bridge`.
- In `python/audio.py`, PCM samples sit in one fixed-size 2000-sample `array('h')` buffer in RAM that is overwritten in place every 125 ms.
- We never write audio to disk, never send audio to the browser, and never include audio in logs or CSV exports.
- Nothing calls the internet at runtime: all CSS, JS, and the area grid SVG are served locally from `assets/`, and records older than `RETENTION_DAYS` (14 days) are automatically deleted from SQLite.

## How to Run It
1. **On a laptop (with fake audio):** Run `python3 night-noise-box/python/main.py --source fake` and open `http://localhost:7000`.
2. **On the UNO Q with fake audio first:** Open the folder in **Arduino App Lab**, enable the **WebUI - HTML** brick on port `7000`, leave `AUDIO_SOURCE = "fake"` in `python/config.py`, and click **Run**. Open `http://<uno-q-ip>:7000`.
3. **Switch profiles & calibrate:** Use **Demo** (`70 / 62 dB`) when testing in a noisy room and **Night** (`52 / 45 dB`) overnight. Hold a phone sound-meter app beside the microphone, type its dB reading into **Loudness Calibration**, and click **Calibrate**; the offset is saved to JSON next to the SQLite database so it survives restarts.
4. **Enable live microphone:** Complete the `TODO(BOARD-CHECK)` hook in `LiveAudioSource.read_window()` (`python/audio.py`) with your Edge Impulse model on the UNO Q and change `AUDIO_SOURCE` to `"live"` in `python/config.py`.
