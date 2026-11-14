# Project: movement-to-music instrument on Arduino UNO Q (hackathon build, ~27 h left)

## What we're building
A device for care homes and rehab clinics that turns movement into music.
One seated person moves their arms; posture sets pitch and harmony, wrist
motion sets rhythm and note onsets. All sensing and AI run on the board.
The board hosts a web page; a laptop or phone opens it, plays the sound
and acts as the screen the player looks at. No cloud, no saved images, no
internet at runtime.

## Hardware and platform (facts, do not change)
- Arduino UNO Q: Linux side (quad-core Cortex-A53, Debian, Python) plus an
  STM32 microcontroller running an Arduino sketch.
- The app is an Arduino App Lab app with this layout:
  sketch/sketch.ino, python/main.py, assets/index.html
- The two sides talk through the Bridge (Arduino_RouterBridge in the sketch,
  arduino.app_utils on the Python side).
- The USB camera is on the Linux side only.
- Modulinos are on the Qwiic port, which is Wire1, on the microcontroller
  side, using the Arduino_Modulino library. We have: Movement (IMU), Buttons,
  Pixels, Knob.
- Sound is NOT produced on the board. The board hosts a web page; a laptop
  or phone on the same local network opens it and plays the audio in the
  browser.
- Measured on our board: pose model runs at [X fps]; camera is
  [model/resolution].

## Architecture (fixed)
- Microcontroller: reads the Movement module at high rate, detects wrist
  "strikes" and tempo, reads Buttons and Knob, drives Pixels. Sends events
  to Linux as they happen.
- Linux: camera pose estimation (arms only, seated), maps arm height to a
  note in a pentatonic scale, owns mode and exercise state, serves the web
  page, and pushes events to the browser over the web UI's live channel.
- Browser: receives events, plays them with the Web Audio API and draws
  the screen. All synthesis happens here.
- Timing rule: note onsets come from the wrist events, never from the
  camera. The camera only chooses which note. Target: under 100 ms from
  strike to sound, measured at the browser. Send small events (note,
  velocity, voice), never audio data.

## Modes
The page is also the screen the player looks at. Two modes, switched from
the page or with a Modulino button:

- Free mode: the instrument. Any movement makes sound. The screen shows a
  simple stick figure of the player's arms and a visual pulse on each note.
- Instruct mode: guided exercises. The screen shows a target arm position
  as a ghost stick figure over the player's own. Matching it within a
  tolerance for a short hold plays a rewarding phrase and moves to the
  next step. A session is a short list of steps; finishing it plays a
  melody and shows a summary (steps completed, time taken).

Instruct mode rules:
- Ship 3-4 preset exercises as plain data in one file (python/exercises.py):
  a list of steps, each with target arm levels, tolerance and hold time.
  All must be doable seated.
- A physio can also record a custom step: hold a pose and press a button.
- Never show failure. No red, no "wrong". Unreached steps just wait, and
  sound keeps responding to movement the whole time.
- The Knob sets tolerance (easier/harder). Pixels show progress through
  the session.
- Large text and shapes, readable from two metres.

## Shared contract (agent 0 writes this first, everyone else reads it)
Create CONTRACT.md defining every message (name, direction, fields, units,
rate). No agent may add or change a message without updating CONTRACT.md.

- Microcontroller <-> Linux (Bridge). Suggested: strike(intensity),
  tempo(bpm), button(id), knob(value), set_pixels(pattern).
- Linux -> browser. Suggested: note_on(note, velocity, voice),
  note_off(voice), target_melody(), state(arm levels, person present),
  mode(free|instruct), step(index, total, target arm levels),
  step_reached(index), session_done(summary). Include a timestamp so
  latency can be logged.
- Browser -> Linux. Suggested: set_mode(mode), start_session(exercise id),
  record_step(), clear_custom().

The board owns the mode and exercise state; the page only displays it and
sends requests.

## Workstreams (one agent each, stay inside your files)
1. Firmware: sketch/sketch.ino. IMU sampling, strike detection with
   debounce, Buttons, Knob, Pixels patterns, Bridge events.
2. Pose: python/pose.py. Camera capture, pose model, smoothed left/right
   arm height as 0..1, a "person present" flag. Frames are never written to
   disk and never leave this module.
3. Sound: assets/synth.js. Web Audio synth in the browser: pentatonic
   voices, a second voice for the other arm, the reward phrase and the
   session melody. Audio must start from a visible "Start" button, because
   browsers block sound until the user clicks. Pre-create the audio nodes
   so the first note isn't late. Include a test page that plays notes from
   keyboard keys with no board connected.
4. Glue and modes: python/main.py and python/exercises.py. Wires pose and
   Bridge to the page; owns mode state; runs instruct sessions (match
   detection with tolerance and hold, custom step recording); fallback
   when pose is slow or absent (free mode on wrist input only).
5. Web UI: assets/index.html. Start button and connection state; mode
   switch; a canvas that draws the stick figure and ghost target from the
   arm levels in state/step messages; the current instruction in one short
   line; the session summary; a small physio panel (pick exercise, record
   custom step, live arm levels). Reconnect automatically if the connection
   drops. No camera image is ever sent to or shown on the page. Plain HTML
   and JS, no framework, no build step.
6. Docs: README.md and a Project Hub draft (components, wiring, how it
   works, how to run). Write plainly and briefly, in first person plural,
   no marketing language.

## Rules
- Do not guess App Lab, Bridge or brick APIs. Read the official docs and
  the built-in examples first and copy their patterns. If something can't
  be confirmed, leave a clearly marked TODO with the question, don't invent
  a signature.
- Check how the Web UI brick sends live messages to the page before
  writing any of it, and use that mechanism. Don't add a second server.
- Keep dependencies minimal. Justify every added package in one line in
  the README.
- Nothing may need the internet at runtime; only the local network between
  board and browser. No CDN links: every script is served from assets/.
- We cannot run your code for you on real hardware in a loop. Every module
  needs a way to run on a laptop with fake input (recorded IMU values, a
  webcam or a video file, simulated arm levels) so it can be checked before
  it goes on the board.
- Small files, no abstractions for later. This has to work on Sunday, not
  scale.

## Done means
- Each module runs standalone with fake input and prints what it would do.
- The page plays correctly from simulated events with no board.
- main.py drives the page end to end on a laptop with a webcam and
  simulated strikes, and prints logged strike-to-sound latency.
- A full instruct session can be completed on a laptop with a webcam, and
  with simulated arm levels and no camera.
- CONTRACT.md matches the code.
- A numbered list of everything that still needs testing on the board.