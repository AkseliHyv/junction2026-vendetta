/**
 * Shared Protocol, Pure IMU Signal Logic, and Linux Mode Coordinator
 * Mirrors `/CONTRACT.md`, `/sketch/sketch.ino`, `/python/exercises.py`, and `/python/main.py`
 * Decoupled from React/DOM for deterministic `node:test` verification.
 */

export type InstrumentMode = 'free' | 'instruct';

export type PixelsPattern =
  | 'idle'
  | 'free_pulse'
  | 'progress_0'
  | 'progress_1'
  | 'progress_2'
  | 'progress_3'
  | 'progress_4'
  | 'progress_5'
  | 'progress_6'
  | 'progress_7'
  | 'progress_8'
  | 'celebrate';

export interface ExerciseStep {
  left: number;
  right: number;
  tolerance: number;
  hold_sec: number;
  instruction: string;
}

export interface ExercisePreset {
  id: string;
  name: string;
  clinicalFocus: string;
  steps: ExerciseStep[];
}

export interface NoteOnMessage {
  note: number;
  velocity: number;
  voice: 0 | 1;
  ts_ms: number;
}

export interface TargetMelodyMessage {
  kind: 'step_reward' | 'session_complete';
  notes: number[];
  ts_ms: number;
}

export interface StateMessage {
  left: number;
  right: number;
  person_present: boolean;
  pose_fps: number;
  fallback_wrist_only: boolean;
  tolerance: number;
  bpm: number;
  hold_progress: number;
  pixels: PixelsPattern;
  ts_ms: number;
}

export interface StepMessage {
  index: number;
  total: number;
  left: number;
  right: number;
  tolerance: number;
  hold_sec: number;
  instruction: string;
  exercise_name: string;
  ts_ms: number;
}

export interface SessionSummary {
  steps_completed: number;
  total_steps: number;
  duration_sec: number;
  exercise_name: string;
  notes_played: number;
  ts_ms: number;
}

export interface ContractWireLog {
  id: number;
  channel: 'MCU -> Linux' | 'Linux -> MCU' | 'Linux -> Browser' | 'Browser -> Linux';
  name: string;
  summary: string;
  ts_ms: number;
}

export const PENTATONIC_MIDI = [60, 62, 64, 67, 69, 72, 74, 76] as const;
export const FALLBACK_ARPEGGIO = [60, 64, 67, 69, 72, 69, 67, 64] as const;
export const REWARD_PHRASE_MIDI = [67, 72, 76];
export const SESSION_MELODY_MIDI = [60, 64, 67, 72, 76, 72, 76];

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

export const PRESET_EXERCISES: ExercisePreset[] = [
  {
    id: 'morning_sun',
    name: 'Morning Sun Reach',
    clinicalFocus: 'Symmetric bilateral shoulder flexion from lap to gentle overhead reach',
    steps: [
      { left: 0.20, right: 0.20, tolerance: 0.17, hold_sec: 1.2, instruction: 'Rest both hands calmly near your lap' },
      { left: 0.55, right: 0.55, tolerance: 0.17, hold_sec: 1.5, instruction: 'Float both arms up to shoulder level' },
      { left: 0.82, right: 0.82, tolerance: 0.18, hold_sec: 1.5, instruction: 'Reach gently toward the warm sun' },
      { left: 0.25, right: 0.25, tolerance: 0.17, hold_sec: 1.2, instruction: 'Lower both hands softly back down' },
    ],
  },
  {
    id: 'wing_lift',
    name: 'Alternating Wing Lift',
    clinicalFocus: 'Independent unilateral arm isolation followed by bilateral opening',
    steps: [
      { left: 0.72, right: 0.20, tolerance: 0.17, hold_sec: 1.4, instruction: 'Lift your left arm while resting your right' },
      { left: 0.20, right: 0.72, tolerance: 0.17, hold_sec: 1.4, instruction: 'Lift your right arm while resting your left' },
      { left: 0.68, right: 0.68, tolerance: 0.17, hold_sec: 1.6, instruction: 'Open both wings wide together' },
    ],
  },
  {
    id: 'gentle_rowing',
    name: 'Gentle Rowing Balance',
    clinicalFocus: 'Cross-midline reciprocal coordination and seated trunk stability',
    steps: [
      { left: 0.45, right: 0.45, tolerance: 0.16, hold_sec: 1.3, instruction: 'Hold both hands steady at chest height' },
      { left: 0.75, right: 0.35, tolerance: 0.17, hold_sec: 1.4, instruction: 'Sweep your left hand higher than your right' },
      { left: 0.35, right: 0.75, tolerance: 0.17, hold_sec: 1.4, instruction: 'Sweep your right hand higher than your left' },
      { left: 0.45, right: 0.45, tolerance: 0.16, hold_sec: 1.3, instruction: 'Return both hands to a calm center' },
    ],
  },
  {
    id: 'crown_and_heart',
    name: 'Crown & Heart Opening',
    clinicalFocus: 'Sustained isometric hold at chest height paired with overhead elevation',
    steps: [
      { left: 0.48, right: 0.48, tolerance: 0.17, hold_sec: 1.4, instruction: 'Bring both hands gently to heart height' },
      { left: 0.85, right: 0.48, tolerance: 0.18, hold_sec: 1.5, instruction: 'Raise your left hand high while right stays steady' },
      { left: 0.48, right: 0.85, tolerance: 0.18, hold_sec: 1.5, instruction: 'Raise your right hand high while left stays steady' },
      { left: 0.82, right: 0.82, tolerance: 0.18, hold_sec: 1.6, instruction: 'Lift both hands together in celebration' },
    ],
  },
];

export function clamp(val: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, val));
}

export function armHeightToMidi(height: number): number {
  const h = clamp(height, 0, 1);
  const idx = Math.min(7, Math.floor(h * 8));
  return PENTATONIC_MIDI[idx];
}

export function midiToNoteName(midi: number): string {
  const m = Math.round(midi);
  const octave = Math.floor(m / 12) - 1;
  const note = NOTE_NAMES[((m % 12) + 12) % 12];
  return `${note}${octave}`;
}

export function midiToFrequency(midi: number): number {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

export function knobToTolerance(knobValue: number): number {
  const k = clamp(Math.round(knobValue), 0, 100);
  return Math.round((0.06 + (k / 100) * 0.22) * 1000) / 1000;
}

/**
 * Exact 200 Hz IMU strike & median-tempo detector from `sketch/sketch.ino`.
 */
export class ImuStrikeDetector {
  private gravityEma = 1.0;
  private prevDyn = 0.0;
  private armedForPeak = false;
  private peakDyn = 0.0;
  private lastStrikeMs = -1000;
  private intervals: number[] = [];

  static readonly DEBOUNCE_MS = 95;
  static readonly TEMPO_TIMEOUT_MS = 2500;
  static readonly GRAVITY_ALPHA = 0.04;
  static readonly THRESHOLD_G = 0.42;
  static readonly MAX_G = 2.2;

  stepSample(
    nowMs: number,
    ax: number,
    ay: number,
    az: number
  ): { strikeIntensity?: number; bpm?: number } {
    const mag = Math.sqrt(ax * ax + ay * ay + az * az);
    this.gravityEma =
      (1 - ImuStrikeDetector.GRAVITY_ALPHA) * this.gravityEma +
      ImuStrikeDetector.GRAVITY_ALPHA * mag;
    const dyn = Math.abs(mag - this.gravityEma);

    if (
      dyn >= ImuStrikeDetector.THRESHOLD_G &&
      nowMs - this.lastStrikeMs >= ImuStrikeDetector.DEBOUNCE_MS
    ) {
      if (!this.armedForPeak) {
        this.armedForPeak = true;
        this.peakDyn = dyn;
      } else if (dyn > this.peakDyn) {
        this.peakDyn = dyn;
      }
    }

    let result: { strikeIntensity?: number; bpm?: number } = {};

    if (this.armedForPeak && dyn < this.prevDyn) {
      this.armedForPeak = false;
      const norm =
        (this.peakDyn - ImuStrikeDetector.THRESHOLD_G) /
        (ImuStrikeDetector.MAX_G - ImuStrikeDetector.THRESHOLD_G);
      const intensity = Math.round(clamp(0.15 + norm * 0.85, 0.05, 1.0) * 1000) / 1000;
      result.strikeIntensity = intensity;

      if (this.lastStrikeMs >= 0) {
        const dt = nowMs - this.lastStrikeMs;
        if (dt >= ImuStrikeDetector.DEBOUNCE_MS && dt <= ImuStrikeDetector.TEMPO_TIMEOUT_MS) {
          this.intervals = [...this.intervals.slice(-3), dt];
          const sorted = [...this.intervals].sort((a, b) => a - b);
          const mid = Math.floor(sorted.length / 2);
          const med =
            sorted.length % 2 === 1
              ? sorted[mid]
              : Math.floor((sorted[mid - 1] + sorted[mid]) / 2);
          if (med > 0) {
            result.bpm = clamp(Math.floor(60000 / med), 30, 220);
          }
        } else if (dt > ImuStrikeDetector.TEMPO_TIMEOUT_MS) {
          this.intervals = [];
        }
      }

      this.lastStrikeMs = nowMs;
      this.peakDyn = 0;
    }

    this.prevDyn = dyn;
    return result;
  }
}

export interface BoardCallbacks {
  onNoteOn?: (msg: NoteOnMessage) => void;
  onTargetMelody?: (msg: TargetMelodyMessage) => void;
  onState?: (msg: StateMessage) => void;
  onStep?: (msg: StepMessage | null) => void;
  onStepReached?: (index: number, total: number) => void;
  onSessionDone?: (summary: SessionSummary) => void;
  onWireLog?: (entry: ContractWireLog) => void;
}

/**
 * Authoritative UNO Q Linux Coordinator (`python/main.py` equivalent).
 */
export class BoardCoordinator {
  mode: InstrumentMode = 'free';
  exerciseId = PRESET_EXERCISES[0].id;
  customSteps: ExerciseStep[] = [];

  left = 0.32;
  right = 0.32;
  personPresent = true;
  poseFps = 24;
  knobValue = 50;
  tolerance = knobToTolerance(50);
  bpm = 92;

  stepIndex = 0;
  sessionActive = false;
  sessionStartMs = 0;
  holdAccumSec = 0;
  notesPlayedInSession = 0;
  totalNotesPlayed = 0;
  pixels: PixelsPattern = 'idle';
  lastSessionSummary: SessionSummary | null = null;

  private arpeggioIdx = 0;
  private logSeq = 1;
  private pulseResetAtMs = 0;
  private callbacks: BoardCallbacks;

  constructor(callbacks: BoardCallbacks = {}) {
    this.callbacks = callbacks;
  }

  setCallbacks(callbacks: BoardCallbacks): void {
    this.callbacks = callbacks;
  }

  private logWire(
    channel: ContractWireLog['channel'],
    name: string,
    summary: string,
    nowMs: number
  ): void {
    this.callbacks.onWireLog?.({
      id: this.logSeq++,
      channel,
      name,
      summary,
      ts_ms: nowMs,
    });
  }

  private setPixels(pattern: PixelsPattern, nowMs: number): void {
    if (this.pixels !== pattern || pattern === 'free_pulse') {
      this.pixels = pattern;
      this.logWire('Linux -> MCU', 'set_pixels', `pattern="${pattern}"`, nowMs);
    }
  }

  getActiveExercise(): { name: string; steps: ExerciseStep[] } {
    if (this.exerciseId === 'custom' && this.customSteps.length > 0) {
      return { name: 'Custom Physio Routine', steps: this.customSteps };
    }
    const found = PRESET_EXERCISES.find((e) => e.id === this.exerciseId) || PRESET_EXERCISES[0];
    return { name: found.name, steps: found.steps };
  }

  getCurrentStepMessage(nowMs = Date.now()): StepMessage | null {
    if (this.mode !== 'instruct' || !this.sessionActive) return null;
    const { name, steps } = this.getActiveExercise();
    const st = steps[this.stepIndex];
    if (!st) return null;
    return {
      index: this.stepIndex,
      total: steps.length,
      left: st.left,
      right: st.right,
      tolerance: this.tolerance,
      hold_sec: st.hold_sec,
      instruction: st.instruction,
      exercise_name: name,
      ts_ms: nowMs,
    };
  }

  /** MCU -> Linux: `strike(intensity)` */
  mcuStrike(intensity: number, nowMs = Date.now()): NoteOnMessage[] {
    const vel = Math.round(clamp(intensity, 0.05, 1.0) * 1000) / 1000;
    this.logWire('MCU -> Linux', 'strike', `intensity=${vel.toFixed(2)}`, nowMs);

    const fallback = !this.personPresent;
    let noteL: number;
    let noteR: number;

    if (fallback) {
      noteL = FALLBACK_ARPEGGIO[this.arpeggioIdx % FALLBACK_ARPEGGIO.length];
      const pIdx = PENTATONIC_MIDI.indexOf(noteL as (typeof PENTATONIC_MIDI)[number]);
      noteR = PENTATONIC_MIDI[(pIdx + 2) % PENTATONIC_MIDI.length];
      this.arpeggioIdx += 1;
    } else {
      noteL = armHeightToMidi(this.left);
      noteR = armHeightToMidi(this.right);
    }

    const emitted: NoteOnMessage[] = [];
    const msg0: NoteOnMessage = { note: noteL, velocity: vel, voice: 0, ts_ms: nowMs };
    emitted.push(msg0);
    this.callbacks.onNoteOn?.(msg0);
    this.notesPlayedInSession += 1;
    this.totalNotesPlayed += 1;

    if (vel >= 0.25 || noteR !== noteL) {
      const msg1: NoteOnMessage = {
        note: noteR,
        velocity: Math.round(vel * 0.82 * 1000) / 1000,
        voice: 1,
        ts_ms: nowMs,
      };
      emitted.push(msg1);
      this.callbacks.onNoteOn?.(msg1);
      this.notesPlayedInSession += 1;
      this.totalNotesPlayed += 1;
    }

    this.logWire(
      'Linux -> Browser',
      'note_on',
      emitted.map((m) => `v${m.voice}:${midiToNoteName(m.note)}`).join(' + '),
      nowMs
    );

    if (this.mode === 'free') {
      this.setPixels('free_pulse', nowMs);
      this.pulseResetAtMs = nowMs + 260;
    }

    return emitted;
  }

  /** MCU -> Linux: `tempo(bpm)` */
  mcuTempo(bpm: number, nowMs = Date.now()): void {
    this.bpm = clamp(Math.round(bpm), 30, 220);
    this.logWire('MCU -> Linux', 'tempo', `bpm=${this.bpm}`, nowMs);
  }

  /** MCU -> Linux: `knob(value)` */
  mcuKnob(value: number, nowMs = Date.now()): void {
    this.knobValue = clamp(Math.round(value), 0, 100);
    const nextTol = knobToTolerance(this.knobValue);
    if (Math.abs(nextTol - this.tolerance) >= 0.001) {
      this.tolerance = nextTol;
      this.logWire(
        'MCU -> Linux',
        'knob',
        `value=${this.knobValue} (tol=±${this.tolerance.toFixed(2)})`,
        nowMs
      );
      if (this.mode === 'instruct' && this.sessionActive) {
        this.callbacks.onStep?.(this.getCurrentStepMessage(nowMs));
      }
    }
  }

  /** MCU -> Linux: `button(id)` */
  mcuButton(buttonId: 0 | 1 | 2, nowMs = Date.now()): void {
    this.logWire('MCU -> Linux', 'button', `id=${buttonId}`, nowMs);
    if (buttonId === 0) {
      this.browserSetMode(this.mode === 'free' ? 'instruct' : 'free', nowMs);
    } else if (buttonId === 1) {
      const ids = PRESET_EXERCISES.map((e) => e.id);
      if (this.customSteps.length > 0) ids.push('custom');
      const idx = ids.indexOf(this.exerciseId);
      const nextId = ids[(idx + 1) % ids.length];
      this.browserStartSession(nextId, nowMs);
    } else if (buttonId === 2) {
      this.browserRecordStep(1.4, nowMs);
    }
  }

  /** Browser -> Linux: `set_mode(mode)` */
  browserSetMode(nextMode: InstrumentMode, nowMs = Date.now()): void {
    this.logWire('Browser -> Linux', 'set_mode', `mode="${nextMode}"`, nowMs);
    this.mode = nextMode;
    if (nextMode === 'instruct') {
      this.startSessionInternal(this.exerciseId, nowMs);
    } else {
      this.sessionActive = false;
      this.holdAccumSec = 0;
      this.lastSessionSummary = null;
      this.setPixels('idle', nowMs);
      this.callbacks.onStep?.(null);
    }
  }

  /** Browser -> Linux: `start_session(exercise_id)` */
  browserStartSession(exerciseId: string, nowMs = Date.now()): void {
    this.logWire('Browser -> Linux', 'start_session', `exercise_id="${exerciseId}"`, nowMs);
    const validId =
      exerciseId === 'custom' && this.customSteps.length === 0
        ? PRESET_EXERCISES[0].id
        : exerciseId;
    this.mode = 'instruct';
    this.startSessionInternal(validId, nowMs);
  }

  private startSessionInternal(exerciseId: string, nowMs: number): void {
    this.exerciseId = exerciseId;
    this.stepIndex = 0;
    this.holdAccumSec = 0;
    this.notesPlayedInSession = 0;
    this.sessionStartMs = nowMs;
    this.sessionActive = true;
    this.lastSessionSummary = null;
    this.setPixels('progress_0', nowMs);
    const stepMsg = this.getCurrentStepMessage(nowMs);
    this.callbacks.onStep?.(stepMsg);
    if (stepMsg) {
      this.logWire(
        'Linux -> Browser',
        'step',
        `1/${stepMsg.total} "${stepMsg.instruction}"`,
        nowMs
      );
    }
  }

  /** Browser -> Linux: `record_step()` */
  browserRecordStep(holdSec = 1.4, nowMs = Date.now()): ExerciseStep {
    const idx = this.customSteps.length + 1;
    const step: ExerciseStep = {
      left: Math.round(this.left * 100) / 100,
      right: Math.round(this.right * 100) / 100,
      tolerance: this.tolerance,
      hold_sec: clamp(holdSec, 0.5, 5.0),
      instruction: `Hold custom pose ${idx} gently and breathe`,
    };
    this.customSteps = [...this.customSteps, step];
    this.logWire(
      'Browser -> Linux',
      'record_step',
      `#${idx} L=${step.left.toFixed(2)} R=${step.right.toFixed(2)}`,
      nowMs
    );
    return step;
  }

  /** Browser -> Linux: `clear_custom()` */
  browserClearCustom(nowMs = Date.now()): void {
    this.customSteps = [];
    if (this.exerciseId === 'custom') {
      this.exerciseId = PRESET_EXERCISES[0].id;
    }
    this.logWire('Browser -> Linux', 'clear_custom', 'cleared custom steps', nowMs);
  }

  /** 15 Hz Pose & Instruct Hold State Evaluation */
  tick(
    dtSec: number,
    pose: { left: number; right: number; personPresent: boolean; fps?: number },
    nowMs = Date.now()
  ): StateMessage {
    this.left = Math.round(clamp(pose.left, 0, 1) * 1000) / 1000;
    this.right = Math.round(clamp(pose.right, 0, 1) * 1000) / 1000;
    this.personPresent = pose.personPresent;
    if (typeof pose.fps === 'number') {
      this.poseFps = pose.fps;
    }

    if (this.mode === 'free' && this.pixels === 'free_pulse' && nowMs >= this.pulseResetAtMs) {
      this.pixels = 'idle';
    }

    let holdRatio = 0;

    if (this.mode === 'instruct' && this.sessionActive) {
      const { name, steps } = this.getActiveExercise();
      const target = steps[this.stepIndex];
      if (target) {
        const matched =
          this.personPresent &&
          Math.abs(this.left - target.left) <= this.tolerance &&
          Math.abs(this.right - target.right) <= this.tolerance;

        if (matched) {
          this.holdAccumSec += Math.max(0, dtSec);
        } else {
          // Never punish wobble: decay hold timer gently at half rate
          this.holdAccumSec = Math.max(0, this.holdAccumSec - Math.max(0, dtSec) * 0.5);
        }

        holdRatio = clamp(this.holdAccumSec / Math.max(0.2, target.hold_sec), 0, 1);

        if (this.holdAccumSec >= target.hold_sec) {
          const reachedIdx = this.stepIndex;
          const total = steps.length;
          this.callbacks.onStepReached?.(reachedIdx, total);
          this.logWire('Linux -> Browser', 'step_reached', `index=${reachedIdx}/${total}`, nowMs);

          this.stepIndex += 1;
          this.holdAccumSec = 0;
          holdRatio = 0;

          const litCount = clamp(Math.round((this.stepIndex / total) * 8), 0, 8);
          this.setPixels(`progress_${litCount}` as PixelsPattern, nowMs);

          if (this.stepIndex >= total) {
            this.sessionActive = false;
            const durationSec = Math.max(0.1, Math.round(((nowMs - this.sessionStartMs) / 1000) * 10) / 10);
            this.setPixels('celebrate', nowMs);

            const melodyMsg: TargetMelodyMessage = {
              kind: 'session_complete',
              notes: [...SESSION_MELODY_MIDI],
              ts_ms: nowMs,
            };
            this.callbacks.onTargetMelody?.(melodyMsg);
            this.logWire('Linux -> Browser', 'target_melody', 'kind="session_complete"', nowMs);

            const summary: SessionSummary = {
              steps_completed: total,
              total_steps: total,
              duration_sec: durationSec,
              exercise_name: name,
              notes_played: this.notesPlayedInSession,
              ts_ms: nowMs,
            };
            this.lastSessionSummary = summary;
            this.callbacks.onStep?.(null);
            this.callbacks.onSessionDone?.(summary);
            this.logWire(
              'Linux -> Browser',
              'session_done',
              `${total} steps in ${durationSec.toFixed(1)}s`,
              nowMs
            );
          } else {
            const rewardMsg: TargetMelodyMessage = {
              kind: 'step_reward',
              notes: [...REWARD_PHRASE_MIDI],
              ts_ms: nowMs,
            };
            this.callbacks.onTargetMelody?.(rewardMsg);
            this.logWire('Linux -> Browser', 'target_melody', 'kind="step_reward"', nowMs);

            const nextStep = this.getCurrentStepMessage(nowMs);
            this.callbacks.onStep?.(nextStep);
            if (nextStep) {
              this.logWire(
                'Linux -> Browser',
                'step',
                `${nextStep.index + 1}/${nextStep.total} "${nextStep.instruction}"`,
                nowMs
              );
            }
          }
        }
      }
    }

    const stateMsg: StateMessage = {
      left: this.left,
      right: this.right,
      person_present: this.personPresent,
      pose_fps: this.poseFps,
      fallback_wrist_only: !this.personPresent,
      tolerance: this.tolerance,
      bpm: this.bpm,
      hold_progress: Math.round(holdRatio * 1000) / 1000,
      pixels: this.pixels,
      ts_ms: nowMs,
    };
    this.callbacks.onState?.(stateMsg);
    return stateMsg;
  }
}
