/*
 * Kinesthesia — Movement-to-Music Instrument for Care Homes & Rehab Clinics
 * Workstream 1: Microcontroller Firmware (Arduino UNO Q — STM32 Zephyr Core)
 *
 * Hardware on Qwiic (Wire1):
 *   - Modulino Movement (LSM6DSOX 6-axis IMU) -> wrist strike onset + tempo
 *   - Modulino Buttons  (Buttons A=0, B=1, C=2) -> mode / session / record step
 *   - Modulino Knob     (Rotary encoder) -> Instruct tolerance 0..100
 *   - Modulino Pixels   (8x addressable RGB LEDs) -> progress & gentle pulse (NO RED)
 *
 * Shared Contract: see /CONTRACT.md
 *   MCU -> Linux (Bridge.notify):
 *     - strike(float intensity)   [0.05 .. 1.00], debounced >= 95 ms
 *     - tempo(int bpm)            [30 .. 220]
 *     - button(int button_id)     {0, 1, 2}
 *     - knob(int value)           [0 .. 100]
 *   Linux -> MCU (Bridge.provide):
 *     - set_pixels(String pattern) {"idle", "free_pulse", "progress_0".."progress_8", "celebrate"}
 */

#ifndef HOST_SIMULATION
#include <Arduino.h>
#include <Wire.h>
#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#endif

// ---------------------------------------------------------------------------
// Constants & Tuning Parameters
// ---------------------------------------------------------------------------
static const unsigned long IMU_INTERVAL_MS     = 5;    // 200 Hz IMU polling
static const unsigned long UI_INTERVAL_MS      = 25;   // 40 Hz Buttons / Knob / Pixels refresh
static const unsigned long STRIKE_DEBOUNCE_MS  = 95;   // Max ~10.5 strikes/s, eliminates double-bounce
static const unsigned long TEMPO_TIMEOUT_MS    = 2500; // Reset tap-tempo chain after 2.5 s pause

// Acceleration is in g (1.0 = Earth gravity at rest).
// Dynamic motion = | ||a|| - gravity_ema |.
static const float GRAVITY_EMA_ALPHA           = 0.04f;
static const float STRIKE_THRESHOLD_G          = 0.42f; // Gentle seated flick triggers cleanly
static const float STRIKE_MAX_G                = 2.20f; // Maps to intensity = 1.00

static const int   NUM_PIXELS                  = 8;
static const int   DEFAULT_BRIGHTNESS          = 18;    // Comfortable indoor care-home brightness (0..100)

#ifndef HOST_SIMULATION
ModulinoMovement movement;
ModulinoButtons  buttons;
ModulinoKnob     knob;
ModulinoPixels   pixels;
#endif

// ---------------------------------------------------------------------------
// State Variables
// ---------------------------------------------------------------------------
static unsigned long lastImuMs       = 0;
static unsigned long lastUiMs        = 0;
static unsigned long lastStrikeMs    = 0;

static float gravityMagEma           = 1.0f;
static float prevDynAccel            = 0.0f;
static bool  armedForPeak            = false;
static float peakDynAccel            = 0.0f;

// Rolling 4-interval strike history for tempo(bpm)
static unsigned long strikeIntervals[4] = {0, 0, 0, 0};
static uint8_t       intervalCount      = 0;
static uint8_t       intervalHead       = 0;

// Button edge-detection state
static bool prevBtnState[3]          = {false, false, false};

// Knob state (clamped 0..100, starts at 50 = medium tolerance)
static int16_t rawKnobOffset         = 0;
static int     lastKnobReported      = -1;

// Pixels animation state (never uses red; warm amber, teal, sky blue, gold)
static String        activePattern   = "idle";
static unsigned long pulseStartMs    = 0;

// ---------------------------------------------------------------------------
// Pure Signal Helpers
// ---------------------------------------------------------------------------
static float clampf(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

static int clampi(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

static int computeMedianBpm() {
  if (intervalCount == 0) return 0;
  unsigned long sorted[4];
  for (uint8_t i = 0; i < intervalCount; i++) {
    sorted[i] = strikeIntervals[i];
  }
  for (uint8_t i = 0; i < intervalCount; i++) {
    for (uint8_t j = i + 1; j < intervalCount; j++) {
      if (sorted[j] < sorted[i]) {
        unsigned long tmp = sorted[i];
        sorted[i] = sorted[j];
        sorted[j] = tmp;
      }
    }
  }
  unsigned long medMs = (intervalCount % 2 == 1)
    ? sorted[intervalCount / 2]
    : (sorted[(intervalCount / 2) - 1] + sorted[intervalCount / 2]) / 2;

  if (medMs == 0) return 0;
  int bpm = (int)(60000UL / medMs);
  return clampi(bpm, 30, 220);
}

// ---------------------------------------------------------------------------
// Modulino Pixels Rendering (Zero Red — Care Home Friendly Palette)
// ---------------------------------------------------------------------------
static void renderPixels(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  pixels.clear();

  if (activePattern == "free_pulse") {
    unsigned long elapsed = nowMs - pulseStartMs;
    if (elapsed < 260) {
      int bright = (int)(DEFAULT_BRIGHTNESS + (260 - elapsed) / 8);
      for (int i = 0; i < NUM_PIXELS; i++) {
        // Warm golden-amber ripple outward from center
        pixels.set(i, 245, 175, 45, clampi(bright, 5, 45));
      }
    } else {
      activePattern = "idle";
    }
  }

  if (activePattern == "idle") {
    // Calm breathing seafoam-teal center glow on LEDs 3 & 4
    int breath = 8 + (int)((nowMs / 120) % 10);
    pixels.set(3, 20, 184, 166, breath);
    pixels.set(4, 20, 184, 166, breath);
  } else if (activePattern.startsWith("progress_")) {
    int lit = activePattern.substring(9).toInt();
    lit = clampi(lit, 0, NUM_PIXELS);
    for (int i = 0; i < NUM_PIXELS; i++) {
      if (i < lit) {
        // Completed/progress LEDs: soothing emerald-teal
        pixels.set(i, 16, 185, 129, DEFAULT_BRIGHTNESS + 6);
      } else {
        // Waiting LEDs: soft warm candle-white (never red)
        pixels.set(i, 180, 170, 145, 5);
      }
    }
  } else if (activePattern == "celebrate") {
    // Gentle alternating gold and sky-blue shimmer
    int phase = (int)((nowMs / 140) % 2);
    for (int i = 0; i < NUM_PIXELS; i++) {
      if ((i + phase) % 2 == 0) {
        pixels.set(i, 250, 190, 50, DEFAULT_BRIGHTNESS + 12);
      } else {
        pixels.set(i, 56, 189, 248, DEFAULT_BRIGHTNESS + 10);
      }
    }
  }

  pixels.show();
#endif
}

// ---------------------------------------------------------------------------
// Bridge RPC Callback: Linux -> MCU
// ---------------------------------------------------------------------------
// TODO(BOARD-CHECK): Confirm whether Arduino_RouterBridge on the UNO Q Zephyr
// STM32 core passes RPC string arguments as Arduino `String` or `std::string`
// when registered via `Bridge.provide("set_pixels", onSetPixels)`.
void onSetPixels(String pattern) {
  activePattern = pattern;
  if (pattern == "free_pulse") {
    pulseStartMs = millis();
  }
}

// ---------------------------------------------------------------------------
// IMU Sampling & Strike Detection
// ---------------------------------------------------------------------------
static void pollImu(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  if (!movement.update()) {
    return;
  }
  float ax = movement.getX();
  float ay = movement.getY();
  float az = movement.getZ();

  float mag = sqrtf(ax * ax + ay * ay + az * az);
  gravityMagEma = (1.0f - GRAVITY_EMA_ALPHA) * gravityMagEma + GRAVITY_EMA_ALPHA * mag;
  float dyn = fabsf(mag - gravityMagEma);

  // Arm peak detector once dynamic acceleration crosses strike threshold
  if (dyn >= STRIKE_THRESHOLD_G && (nowMs - lastStrikeMs) >= STRIKE_DEBOUNCE_MS) {
    if (!armedForPeak) {
      armedForPeak = true;
      peakDynAccel = dyn;
    } else if (dyn > peakDynAccel) {
      peakDynAccel = dyn;
    }
  }

  // Trigger immediately on crest (first downward slope after threshold crossing)
  // Adds <= 5 ms crest latency while giving accurate velocity expression.
  if (armedForPeak && dyn < prevDynAccel) {
    armedForPeak = false;

    float norm = (peakDynAccel - STRIKE_THRESHOLD_G) / (STRIKE_MAX_G - STRIKE_THRESHOLD_G);
    float intensity = clampf(0.15f + norm * 0.85f, 0.05f, 1.00f);

    // Fire-and-forget notification to Linux for sub-100ms note onset
    Bridge.notify("strike", intensity);

    // Trigger immediate local tactile LED feedback in Free mode
    if (activePattern == "idle" || activePattern == "free_pulse") {
      activePattern = "free_pulse";
      pulseStartMs = nowMs;
    }

    // Update rolling tap-tempo if consecutive strike within 2.5 s
    if (lastStrikeMs > 0) {
      unsigned long dt = nowMs - lastStrikeMs;
      if (dt >= STRIKE_DEBOUNCE_MS && dt <= TEMPO_TIMEOUT_MS) {
        strikeIntervals[intervalHead] = dt;
        intervalHead = (intervalHead + 1) % 4;
        if (intervalCount < 4) intervalCount++;
        int bpm = computeMedianBpm();
        if (bpm > 0) {
          Bridge.notify("tempo", bpm);
        }
      } else if (dt > TEMPO_TIMEOUT_MS) {
        intervalCount = 0;
        intervalHead = 0;
      }
    }

    lastStrikeMs = nowMs;
    peakDynAccel = 0.0f;
  }

  prevDynAccel = dyn;
#endif
}

// ---------------------------------------------------------------------------
// Buttons & Rotary Knob Polling
// ---------------------------------------------------------------------------
static void pollControls() {
#ifndef HOST_SIMULATION
  // 1. Modulino Buttons (0 = Mode, 1 = Start/Next Exercise, 2 = Record Custom Pose)
  if (buttons.update()) {
    for (int i = 0; i < 3; i++) {
      bool pressed = buttons.isPressed(i);
      if (pressed && !prevBtnState[i]) {
        Bridge.notify("button", i);
      }
      prevBtnState[i] = pressed;
    }
    // Keep subtle button guide LEDs lit so care-home staff can see A / B / C
    buttons.setLeds(true, true, true);
  }

  // 2. Modulino Knob -> normalized 0..100 tolerance control
  int raw = (int)knob.get();
  int mapped = clampi(50 + (raw - rawKnobOffset) * 2, 0, 100);
  // Adjust offset so twisting past ends doesn't wind up infinitely
  if (mapped == 0 && (50 + (raw - rawKnobOffset) * 2) < 0) {
    rawKnobOffset = raw + 25;
  } else if (mapped == 100 && (50 + (raw - rawKnobOffset) * 2) > 100) {
    rawKnobOffset = raw - 25;
  }

  if (abs(mapped - lastKnobReported) >= 1) {
    lastKnobReported = mapped;
    Bridge.notify("knob", mapped);
  }
#endif
}

// ---------------------------------------------------------------------------
// Arduino Lifecycle Entry Points
// ---------------------------------------------------------------------------
#ifndef HOST_SIMULATION
void setup() {
  Bridge.begin();
  Bridge.provide("set_pixels", onSetPixels);

  // UNO Q Qwiic connector lives on Wire1
  Wire1.begin();
  Modulino.begin(Wire1);

  movement.begin();
  buttons.begin();
  knob.begin();
  pixels.begin();

  buttons.setLeds(true, true, true);
  rawKnobOffset = (int)knob.get();

  // Report initial centered knob tolerance (50 -> 0.17 tolerance)
  Bridge.notify("knob", 50);
  lastKnobReported = 50;
  renderPixels(millis());
}

void loop() {
  unsigned long nowMs = millis();

  if (nowMs - lastImuMs >= IMU_INTERVAL_MS) {
    lastImuMs = nowMs;
    pollImu(nowMs);
  }

  if (nowMs - lastUiMs >= UI_INTERVAL_MS) {
    lastUiMs = nowMs;
    pollControls();
    renderPixels(nowMs);
  }
}
#endif
