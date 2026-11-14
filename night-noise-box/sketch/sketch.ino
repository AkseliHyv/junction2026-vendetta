/*
 * Night-Noise Box — Microcontroller Firmware (Arduino UNO Q — STM32 Zephyr Core)
 *
 * Hardware on Qwiic (Wire1):
 *   - Modulino Pixels   (8x addressable RGB LEDs) -> live dB light bar + "no signal" pattern
 *   - Modulino Buttons  (Buttons A/B/C)           -> "woke me" marker on press
 *   - Modulino Movement (LSM6DSOX 6-axis IMU)     -> ~20 Hz box tamper detection
 *   - Modulino Knob     (Rotary encoder)          -> left in place unused
 *
 * Shared Contract: see /CONTRACT.md
 *   Linux -> MCU (Bridge.provide):
 *     - level(float db)                     [~8 Hz calibrated loudness in dB]
 *     - label(String name, float confidence)[sent when label changes]
 *     - set_thresholds(float start_db, float end_db, int start_hold_ms, int end_hold_ms)
 *   MCU -> Linux (Bridge.notify):
 *     - event_start()
 *     - event_end(int duration_ms)
 *     - woke_me()
 *     - tamper()
 *     - heartbeat()                         [1 Hz MCU liveness signal]
 *     - mcu_level(float db)                 [optional, unused by default]
 *     - knob(int value)                     [legacy unused, left in place]
 */

#ifndef HOST_SIMULATION
#include <Arduino.h>
#include <Wire.h>
#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#endif

// ---------------------------------------------------------------------------
// Named Event Thresholds & Hold Constants (same dB units as level(db))
// ---------------------------------------------------------------------------
static const float         START_THRESHOLD_DB   = 52.0f;  // Event starts above this dB
static const float         END_THRESHOLD_DB     = 45.0f;  // Event ends below this dB (< START)
static const unsigned long START_HOLD_MS        = 375UL;  // Must stay >= START_THRESHOLD_DB
static const unsigned long END_HOLD_MS          = 625UL;  // Must stay <= END_THRESHOLD_DB
static const unsigned long NO_SIGNAL_TIMEOUT_MS = 2000UL; // 2 s watchdog if level(db) stops

// Loudness Light-Bar Mapping Constants (same dB units as level(db))
static const float         LEVEL_BAR_MIN_DB     = 30.0f;  // Quiet room baseline -> 0 LEDs
static const float         LEVEL_BAR_MAX_DB     = 78.0f;  // Loud night event    -> 8 LEDs

// Polling, Heartbeat & Debounce Intervals
static const unsigned long IMU_INTERVAL_MS       = 50UL;   // ~20 Hz Movement polling for tamper
static const unsigned long UI_INTERVAL_MS        = 25UL;   // 40 Hz Buttons / Pixels / Watchdog
static const unsigned long HEARTBEAT_INTERVAL_MS = 1000UL; // 1 Hz heartbeat() to Linux
static const unsigned long WOKE_DEBOUNCE_MS      = 200UL;  // Button debounce for woke_me()
static const unsigned long TAMPER_DEBOUNCE_MS    = 1000UL; // Minimum gap between tamper() alerts

// Acceleration in g (1.0 = Earth gravity at rest); ~20 Hz EMA filter
static const float         GRAVITY_EMA_ALPHA    = 0.15f;
static const float         TAMPER_THRESHOLD_G   = 0.28f;  // Box nudge / pickup threshold

static const int           NUM_PIXELS           = 8;
static const int           DEFAULT_BRIGHTNESS   = 14;     // Bedroom-safe dim night brightness

#ifndef HOST_SIMULATION
ModulinoMovement movement;
ModulinoButtons  buttons;
ModulinoKnob     knob;
ModulinoPixels   pixels;
#endif

// ---------------------------------------------------------------------------
// State Variables
// ---------------------------------------------------------------------------
static unsigned long lastImuMs          = 0;
static unsigned long lastUiMs           = 0;
static unsigned long lastHeartbeatMs    = 0;
static unsigned long lastWokeMs         = 0;
static unsigned long lastTamperMs       = 0;
static bool          hasWokeOnce        = false;
static bool          hasTamperOnce      = false;

// Runtime Thresholds (initialized from named defaults; updated via set_thresholds)
static float         activeStartDb      = START_THRESHOLD_DB;
static float         activeEndDb        = END_THRESHOLD_DB;
static unsigned long activeStartHoldMs  = START_HOLD_MS;
static unsigned long activeEndHoldMs    = END_HOLD_MS;

// Signal & Watchdog State
static float         currentLevelDb     = 0.0f;
static String        currentLabel       = "background";
static float         currentConfidence  = 0.0f;
static bool          hasSignal          = false;
static unsigned long lastLevelRxMs      = 0;

// Hysteresis Event State
static bool          eventOpen          = false;
static unsigned long eventStartMs       = 0;
static bool          aboveStartArmed    = false;
static unsigned long aboveStartSinceMs  = 0;
static bool          belowEndArmed      = false;
static unsigned long belowEndSinceMs    = 0;

// Movement State
static float         gravityMagEma     = 1.0f;
static bool          imuInitialized    = false;

// Button edge-detection state
static bool          prevBtnState[3]   = {false, false, false};

// Legacy Knob state (left in place unused; do not build on it yet)
static int16_t       rawKnobOffset     = 0;
static int           lastKnobReported  = -1;

// ---------------------------------------------------------------------------
// Pure Helpers
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

static int levelDbToLitLeds(float db) {
  if (db <= LEVEL_BAR_MIN_DB) return 0;
  float ratio = (db - LEVEL_BAR_MIN_DB) / (LEVEL_BAR_MAX_DB - LEVEL_BAR_MIN_DB);
  int lit = (int)(clampf(ratio, 0.0f, 1.0f) * NUM_PIXELS + 0.999f);
  return clampi(lit, 0, NUM_PIXELS);
}

// ---------------------------------------------------------------------------
// Event Hysteresis & 2-Second Watchdog
// ---------------------------------------------------------------------------
static void updateEventMachine(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  // 1. Watchdog: no level(db) received for >= 2 seconds
  if (hasSignal && (nowMs - lastLevelRxMs) >= NO_SIGNAL_TIMEOUT_MS) {
    hasSignal = false;
    aboveStartArmed = false;
    belowEndArmed = false;
    if (eventOpen) {
      int durationMs = (int)(nowMs - eventStartMs);
      eventOpen = false;
      Bridge.notify("event_end", durationMs);
    }
    return;
  }

  if (!hasSignal) {
    return;
  }

  // 2. Hysteresis transitions when signal is live (uses runtime active thresholds)
  if (!eventOpen) {
    if (currentLevelDb >= activeStartDb) {
      if (!aboveStartArmed) {
        aboveStartArmed = true;
        aboveStartSinceMs = nowMs;
      } else if ((nowMs - aboveStartSinceMs) >= activeStartHoldMs) {
        eventOpen = true;
        eventStartMs = nowMs;
        aboveStartArmed = false;
        belowEndArmed = false;
        Bridge.notify("event_start");
      }
    } else {
      aboveStartArmed = false;
    }
  } else {
    if (currentLevelDb <= activeEndDb) {
      if (!belowEndArmed) {
        belowEndArmed = true;
        belowEndSinceMs = nowMs;
      } else if ((nowMs - belowEndSinceMs) >= activeEndHoldMs) {
        int durationMs = (int)(nowMs - eventStartMs);
        eventOpen = false;
        belowEndArmed = false;
        Bridge.notify("event_end", durationMs);
      }
    } else {
      belowEndArmed = false;
    }
  }
#endif
}

// ---------------------------------------------------------------------------
// Modulino Pixels Rendering: Loudness Light Bar vs. "No Signal" Pattern
// ---------------------------------------------------------------------------
static void renderPixels(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  pixels.clear();

  if (!hasSignal) {
    // Distinct "no signal" pattern: slow alternating outer endpoint LEDs (0 & 7)
    bool phase = ((nowMs / 400UL) % 2UL) == 0UL;
    if (phase) {
      pixels.set(0, 120, 90, 220, DEFAULT_BRIGHTNESS);
      pixels.set(NUM_PIXELS - 1, 40, 30, 90, 6);
    } else {
      pixels.set(0, 40, 30, 90, 6);
      pixels.set(NUM_PIXELS - 1, 120, 90, 220, DEFAULT_BRIGHTNESS);
    }
    pixels.show();
    return;
  }

  int lit = levelDbToLitLeds(currentLevelDb);
  for (int i = 0; i < lit; i++) {
    if (i < 4) {
      // Quiet/background range: calm seafoam-teal
      pixels.set(i, 20, 184, 166, DEFAULT_BRIGHTNESS);
    } else if (i < 6) {
      // Moderate noise range: warm amber
      pixels.set(i, 245, 175, 45, DEFAULT_BRIGHTNESS + 4);
    } else {
      // High night noise above threshold: bright gold-orange
      pixels.set(i, 249, 115, 22, DEFAULT_BRIGHTNESS + 8);
    }
  }

  pixels.show();
#endif
}

// ---------------------------------------------------------------------------
// Bridge RPC Callbacks: Linux -> MCU
// ---------------------------------------------------------------------------
void onLevel(float db) {
#ifndef HOST_SIMULATION
  unsigned long nowMs = millis();
  currentLevelDb = clampf(db, 0.0f, 120.0f);
  hasSignal = true;
  lastLevelRxMs = nowMs;
  updateEventMachine(nowMs);
#endif
}

// TODO(BOARD-CHECK): Confirm whether Arduino_RouterBridge on the UNO Q Zephyr
// STM32 core passes RPC string arguments as Arduino `String` or `std::string`
// when registered via `Bridge.provide("label", onLabel)`.
void onLabel(String name, float confidence) {
  currentLabel = name;
  currentConfidence = clampf(confidence, 0.0f, 1.0f);
}

void onSetThresholds(float startDb, float endDb, int startHoldMs, int endHoldMs) {
  float safeStart = clampf(startDb, 5.0f, 120.0f);
  float safeEnd = clampf(endDb, 0.0f, safeStart - 1.0f);
  activeStartDb = safeStart;
  activeEndDb = safeEnd;
  activeStartHoldMs = (unsigned long)(startHoldMs < 0 ? 0 : startHoldMs);
  activeEndHoldMs = (unsigned long)(endHoldMs < 0 ? 0 : endHoldMs);
  aboveStartArmed = false;
  belowEndArmed = false;
}

// ---------------------------------------------------------------------------
// ~20 Hz IMU Tamper Polling
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
  if (!imuInitialized) {
    gravityMagEma = mag;
    imuInitialized = true;
    return;
  }

  gravityMagEma = (1.0f - GRAVITY_EMA_ALPHA) * gravityMagEma + GRAVITY_EMA_ALPHA * mag;
  float dyn = fabsf(mag - gravityMagEma);

  if (dyn >= TAMPER_THRESHOLD_G && (!hasTamperOnce || (nowMs - lastTamperMs) >= TAMPER_DEBOUNCE_MS)) {
    hasTamperOnce = true;
    lastTamperMs = nowMs;
    Bridge.notify("tamper");
  }
#endif
}

// ---------------------------------------------------------------------------
// Buttons ("woke me") & Legacy Unused Knob Polling
// ---------------------------------------------------------------------------
static void pollControls(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  // 1. Modulino Buttons: any button press marks "this woke me up"
  if (buttons.update()) {
    for (int i = 0; i < 3; i++) {
      bool pressed = buttons.isPressed(i);
      if (pressed && !prevBtnState[i]) {
        if (!hasWokeOnce || (nowMs - lastWokeMs) >= WOKE_DEBOUNCE_MS) {
          hasWokeOnce = true;
          lastWokeMs = nowMs;
          Bridge.notify("woke_me");
        }
      }
      prevBtnState[i] = pressed;
    }
    buttons.setLeds(true, false, false);
  }

  // 2. Modulino Knob (left in place unused; do not build on it yet)
  int raw = (int)knob.get();
  int mapped = clampi(50 + (raw - rawKnobOffset) * 2, 0, 100);
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
  Bridge.provide("level", onLevel);
  Bridge.provide("label", onLabel);
  Bridge.provide("set_thresholds", onSetThresholds);

  // UNO Q Qwiic connector lives on Wire1
  Wire1.begin();
  Modulino.begin(Wire1);

  movement.begin();
  buttons.begin();
  knob.begin();
  pixels.begin();

  buttons.setLeds(true, false, false);
  rawKnobOffset = (int)knob.get();
  lastKnobReported = 50;

  unsigned long bootMs = millis();
  lastHeartbeatMs = bootMs;
  Bridge.notify("heartbeat");
  renderPixels(bootMs);
}

void loop() {
  unsigned long nowMs = millis();

  if (nowMs - lastHeartbeatMs >= HEARTBEAT_INTERVAL_MS) {
    lastHeartbeatMs = nowMs;
    Bridge.notify("heartbeat");
  }

  if (nowMs - lastImuMs >= IMU_INTERVAL_MS) {
    lastImuMs = nowMs;
    pollImu(nowMs);
  }

  if (nowMs - lastUiMs >= UI_INTERVAL_MS) {
    lastUiMs = nowMs;
    pollControls(nowMs);
    updateEventMachine(nowMs);
    renderPixels(nowMs);
  }
}
#endif
