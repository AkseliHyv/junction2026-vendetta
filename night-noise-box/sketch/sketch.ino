/*
 * STM32 side of the Night-Noise Box.
 *
 * Modulinos on the Qwiic port (Wire1):
 *   Pixels    loudness bar, or a blinking "no signal" pattern
 *   Buttons   any press sends woke_me()
 *   Movement  sends tamper() when the box is moved
 *   Knob      read but not used
 *
 * From Linux:  level(db), label(name, confidence),
 *              set_thresholds(start_db, end_db, start_hold_ms, end_hold_ms)
 * To Linux:    event_start(), event_end(duration_ms), woke_me(), tamper(),
 *              heartbeat() once a second, knob(value)
 *
 * In analysis mode Linux only sends level(), so this just drives the LED bar.
 * python/sim_imu_replay.py mirrors this logic for tests; keep them in sync.
 */

#ifndef HOST_SIMULATION
#include <Arduino.h>
#include <Wire.h>
#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#endif

// --- Settings ---
// Event thresholds, used until Linux sends set_thresholds().
static const float         START_THRESHOLD_DB   = 52.0f;
static const float         END_THRESHOLD_DB     = 45.0f;
static const unsigned long START_HOLD_MS        = 375UL;  // how long it must stay above start
static const unsigned long END_HOLD_MS          = 625UL;  // how long it must stay below end
static const unsigned long NO_SIGNAL_TIMEOUT_MS = 2000UL; // no level() for this long = no signal

static const float         LEVEL_BAR_MIN_DB     = 30.0f;  // 0 LEDs lit
static const float         LEVEL_BAR_MAX_DB     = 78.0f;  // all 8 lit

static const unsigned long IMU_INTERVAL_MS       = 50UL;
static const unsigned long UI_INTERVAL_MS        = 25UL;
static const unsigned long HEARTBEAT_INTERVAL_MS = 1000UL;
static const unsigned long WOKE_DEBOUNCE_MS      = 200UL;
static const unsigned long TAMPER_DEBOUNCE_MS    = 1000UL;

// Tamper = total acceleration differs from its slow average by this many g.
static const float         GRAVITY_EMA_ALPHA    = 0.15f;
static const float         TAMPER_THRESHOLD_G   = 0.28f;

static const int           NUM_PIXELS           = 8;
static const int           DEFAULT_BRIGHTNESS   = 14;     // dim enough for a bedroom

#ifndef HOST_SIMULATION
ModulinoMovement movement;
ModulinoButtons  buttons;
ModulinoKnob     knob;
ModulinoPixels   pixels;
#endif

// --- State ---
static unsigned long lastImuMs          = 0;
static unsigned long lastUiMs           = 0;
static unsigned long lastHeartbeatMs    = 0;
static unsigned long lastWokeMs         = 0;
static unsigned long lastTamperMs       = 0;
static bool          hasWokeOnce        = false;
static bool          hasTamperOnce      = false;

static float         activeStartDb      = START_THRESHOLD_DB;
static float         activeEndDb        = END_THRESHOLD_DB;
static unsigned long activeStartHoldMs  = START_HOLD_MS;
static unsigned long activeEndHoldMs    = END_HOLD_MS;

static float         currentLevelDb     = 0.0f;
static String        currentLabel       = "background";
static float         currentConfidence  = 0.0f;
static bool          hasSignal          = false;
static unsigned long lastLevelRxMs      = 0;

static bool          eventOpen          = false;
static unsigned long eventStartMs       = 0;
static bool          aboveStartArmed    = false;
static unsigned long aboveStartSinceMs  = 0;
static bool          belowEndArmed      = false;
static unsigned long belowEndSinceMs    = 0;

static float         gravityMagEma     = 1.0f;
static bool          imuInitialized    = false;

static bool          prevBtnState[3]   = {false, false, false};

static int16_t       rawKnobOffset     = 0;
static int           lastKnobReported  = -1;

// --- Helpers ---
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

// --- Event detection ---
static void updateEventMachine(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  // Linux stopped sending levels: close any open event.
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

// --- LEDs ---
static void renderPixels(unsigned long nowMs) {
#ifndef HOST_SIMULATION
  pixels.clear();

  if (!hasSignal) {
    // No signal: the two end LEDs blink alternately.
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
      pixels.set(i, 20, 184, 166, DEFAULT_BRIGHTNESS);
    } else if (i < 6) {
      pixels.set(i, 245, 175, 45, DEFAULT_BRIGHTNESS + 4);
    } else {
      pixels.set(i, 249, 115, 22, DEFAULT_BRIGHTNESS + 8);
    }
  }

  pixels.show();
#endif
}

// --- Messages from Linux ---
void onLevel(float db) {
#ifndef HOST_SIMULATION
  unsigned long nowMs = millis();
  currentLevelDb = clampf(db, 0.0f, 120.0f);
  hasSignal = true;
  lastLevelRxMs = nowMs;
  updateEventMachine(nowMs);
#endif
}

// If the Bridge library won't compile this, try std::string instead of String.
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

// --- Tamper detection ---
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

// --- Buttons and knob ---
static void pollControls(unsigned long nowMs) {
#ifndef HOST_SIMULATION
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

// --- setup / loop ---
#ifndef HOST_SIMULATION
void setup() {
  Bridge.begin();
  Bridge.provide("level", onLevel);
  Bridge.provide("label", onLabel);
  Bridge.provide("set_thresholds", onSetThresholds);

  // The Qwiic connector is on Wire1.
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
