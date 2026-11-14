# Night-Noise Box

A noise monitor built on the Arduino UNO Q. Each box listens through a USB
microphone, measures how loud it is and uses an Edge Impulse model to guess
what's making the noise (traffic, voices, construction...). Every few seconds
it uploads those numbers to a server, which draws every box on a world map.

Audio never leaves the box and is never written to disk. Only the summary
numbers are sent.

```
 UNO Q box                                    your computer
┌─────────────────────────────────┐          ┌───────────────────────────┐
│ USB mic → python/analysis.py    │  HTTP    │ server/server.py          │
│           level, L90, sources ──┼─────────►│   stores readings         │
│              │                  │          │   serves the map on :8080 │
│              ▼ Bridge           │          └───────────────────────────┘
│ STM32 sketch → LED loudness bar │
└─────────────────────────────────┘
```

## What you need

- Arduino UNO Q
- A USB microphone (a USB webcam's mic works)
- Modulino Pixels on the Qwiic port, for the LED loudness bar (optional)
- A computer on the same network as the board, to run the server

Everything on the computer side is the Python 3 standard library. There's
nothing to install.

## Running it

### 1. Start the server

```bash
python3 server/server.py --no-seed
```

Open http://localhost:8080. The map is empty until a box uploads something.
Add `?demo` to the URL to see it with made-up data.

Find your computer's address on the network with `hostname -I`. The board
needs it in the next step. If the board can't connect, check that port 8080
isn't blocked by a firewall, and that the network doesn't isolate devices from
each other (guest Wi-Fi often does; a phone hotspot is an easy workaround).

### 2. Configure the box

Edit `night-noise-box/python/config.py`:

```python
DEVICE_ID = "box-01"                                       # different for each box
DEVICE_LAT = 60.1868                                       # where the box is
DEVICE_LON = 24.8274
READINGS_URL = "http://<your computer's IP>:8080/api/readings"
```

App Lab starts the app without command-line arguments, so `config.py` is the
only way to configure the board.

### 3. Copy the app to the board and run it

```bash
night-noise-box/deploy.sh <board IP or hostname>
```

This copies the app over SSH to `/home/arduino/ArduinoApps/night-noise-box`.
Then open **Night-Noise Box** in Arduino App Lab and press **Run**. App Lab
builds and flashes the sketch to the STM32 and starts `python/main.py`.

After any change, run `deploy.sh` again and press **Run** again.

Within a few seconds the server prints a line for each upload, and the box
appears on the map.

### Checking the board

From an SSH session on the board:

```bash
cd ~/ArduinoApps/night-noise-box
python3 python/mic.py --list          # which capture device "auto" picks
python3 python/mic.py --seconds 3     # live levels, to check the mic works
python3 python/classifier.py          # whether the model loads
python3 python/analysis.py --once     # one analysis, printed and uploaded
```

## The model

The Edge Impulse model goes in `night-noise-box/models/`. See
[models/README.md](night-noise-box/models/README.md). Without a model the box
still measures and uploads loudness, but can't say what the noise is.

To rename the model's labels before upload (say `car` to `traffic`), use
`EI_LABEL_MAP` in `config.py`.

## Running without a board

```bash
python3 server/server.py --no-seed
python3 night-noise-box/python/analysis.py --mic synthetic \
    --readings-url http://localhost:8080/api/readings
```

The synthetic mic plays a quiet hiss with a tone every few seconds. Change
`DEVICE_LAT`/`DEVICE_LON` in `config.py` to move the box on the map.

To fill the map with 36 simulated boxes around Helsinki and Espoo (or
thousands, for load testing), see [mock-devices/README.md](mock-devices/README.md):

```bash
python3 server/server.py --no-seed --db /tmp/noise_map_mock.sqlite3
python3 mock-devices/mock_fleet.py --backfill-hours 24
```

Self-tests, none of which need hardware:

```bash
python3 night-noise-box/python/analysis.py --self-test
python3 night-noise-box/python/audio.py --self-test
python3 night-noise-box/python/store.py --self-test
python3 night-noise-box/python/sim_imu_replay.py
python3 night-noise-box/python/main.py --self-test
```

## Calibration

The box measures dBFS (relative to the mic's maximum) and adds
`CALIBRATION_OFFSET_DB` to get dB. The default of 94 is a rough guess. To get
real numbers, compare the box with a sound meter app on your phone in a steady
noise and adjust the offset by the difference.

## Events mode

`RUN_MODE = "events"` in `config.py` switches to the project's first design,
built for a bedside box. The STM32 decides when a noise event starts and ends,
a Modulino Buttons press marks "this woke me up", Modulino Movement flags the
box being moved, and the board serves its own page with heatmaps of the
night. Events can also be uploaded to the server's `/api/events`, but the map
doesn't show them.

It only runs on simulated audio: the live mic was never connected to this mode
(`LiveAudioSource` in `audio.py`). To try it on a laptop:

```bash
python3 night-noise-box/python/main.py --mode events --source fake
```

and open http://localhost:7000. On the board the page is served by App Lab's
WebUI brick.

## Layout

```
night-noise-box/          the App Lab app that runs on the board
  app.yaml                App Lab app definition
  deploy.sh               copies the app to the board
  sketch/                 STM32 firmware (LED bar; event detection in events mode)
  python/
    main.py               entry point; picks analysis or events mode
    config.py             all settings
    analysis.py           measures and uploads (analysis mode)
    mic.py                reads the USB mic through arecord
    classifier.py         runs the Edge Impulse model
    uploader.py           background uploads with retry
    audio.py, store.py    events mode: simulated audio, SQLite storage
    sim_imu_replay.py     Python copy of the sketch, for testing without a board
  assets/                 the local page (events mode)
  models/                 the Edge Impulse model
mock-devices/             simulated boxes that upload like real ones (load testing)
server/
  server.py               receives uploads and serves the map
  static/                 the map (see static/README.md)
PROTOCOL.md               message formats between the parts
```
