# Arduino Project Hub Draft: Movement-to-Music Rehab Instrument on UNO Q

## Overview
We built an offline movement-to-music instrument for care homes and physical rehabilitation clinics using the Arduino UNO Q and four Modulino nodes. A seated player wears the Modulino Movement IMU on their wrist while sitting in front of a USB camera connected to the UNO Q. Arm height chooses notes from a C major pentatonic scale, and gentle wrist flicks trigger immediate musical notes in a web browser over the local network.

Everything runs locally on the board and browser. We never connect to the cloud, we never save camera frames to disk, and we never send camera images to the browser.

## Components Used
- 1x **Arduino UNO Q** (Linux Cortex-A53 MPU + STM32 Zephyr MCU)
- 1x **Arduino Modulino Movement** (LSM6DSOX 6-axis IMU, worn on a soft wrist strap)
- 1x **Arduino Modulino Buttons** (3 push-buttons with LEDs for mode switch, exercise selection, and pose recording)
- 1x **Arduino Modulino Knob** (rotary encoder for adjusting pose-matching tolerance)
- 1x **Arduino Modulino Pixels** (8 addressable RGB LEDs for calm progress and strike feedback)
- 4x **Qwiic I2C cables** (including one long flexible cable for the wrist sensor)
- 1x **USB UVC Webcam** (connected to the UNO Q Linux host port)
- 1x **Laptop, tablet, or phone** on the same local Wi-Fi network (for screen display and Web Audio playback)

## Wiring
1. Plug a Qwiic cable into the **Qwiic (`Wire1`)** port on the Arduino UNO Q.
2. Daisy-chain **Modulino Buttons** -> **Modulino Knob** -> **Modulino Pixels** on the table so a physiotherapist or care worker can reach them easily.
3. Connect **Modulino Movement** at the end of the Qwiic chain using a flexible cable and attach it to a comfortable wrist band.
4. Plug the USB webcam into the UNO Q USB host port and aim it at chest height toward the player's chair from roughly 1.8 to 2.0 metres away.

## How It Works
We split the work across the three parts of the system so note timing stays crisp (under 100 ms from wrist motion to sound):

1. **STM32 Microcontroller (`sketch/sketch.ino`):** Polls `ModulinoMovement` at 200 Hz, removes gravity with a lightweight exponential filter, detects wrist strikes with a 95 ms debounce, and calculates rolling median tempo in BPM. It also reads `ModulinoButtons` and `ModulinoKnob` and updates `ModulinoPixels`. Events are sent immediately to Linux via `Bridge.notify`.
2. **Linux Microprocessor (`python/main.py`, `python/pose.py`, `python/exercises.py`):** Reads `320x240` frames from the USB camera in memory, extracts shoulder and wrist heights (`0.0` at lap to `1.0` overhead), and discards the frame immediately. When a `strike` arrives from the STM32, Linux pairs the latest arm heights with C major pentatonic notes and pushes small JSON events (`note_on`, `state`, `step`, `session_done`) through the App Lab `WebUI` brick over Socket.IO.
3. **Browser Screen & Synth (`assets/index.html`, `assets/synth.js`):** Pre-allocates a Web Audio API voice pool when the user clicks **Start Audio** (a warm marimba voice for the left arm and a soft bell voice for the right arm). It draws a high-contrast stick figure readable from two metres away.

### Two Modes for Care and Play
- **Free Mode:** Every wrist motion plays harmonious pentatonic notes. If the camera is unplugged or nobody is in frame, the board automatically falls back to a soothing wrist-driven arpeggio so music never stops.
- **Instruct Mode:** Displays a dashed ghost stick figure for seated exercises (`Morning Sun Reach`, `Alternating Wing Lift`, `Gentle Rowing Balance`, `Crown & Heart Opening`, plus custom poses recorded live by a physiotherapist pressing Button C). Matching the pose within the Knob's tolerance fills a calm progress ring, plays an uplifting phrase, and advances the Modulino Pixels. We never show red or "wrong" states—unreached poses wait patiently while movement keeps making music.

## How to Run It
1. Clone this repository into **Arduino App Lab** on the Arduino UNO Q and ensure the **WebUI - HTML** brick is enabled on port `7000`.
2. Click **Run** in App Lab to flash `sketch/sketch.ino` to the STM32 and start `python/main.py` on Debian Linux.
3. On a laptop or tablet on the same local network, open `http://<uno-q-ip>:7000` and click **Start Audio**.
4. Use **Button A** (or the screen toggle) to switch between **Free Mode** and **Instruct Mode**, **Button B** to start the next seated exercise, **Button C** to record a custom pose step, and the **Knob** to make matching gentler or more precise.
