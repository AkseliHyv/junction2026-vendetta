import test from 'node:test';
import assert from 'node:assert/strict';
import {
  armHeightToMidi,
  BoardCoordinator,
  ImuStrikeDetector,
  knobToTolerance,
  midiToNoteName,
  PENTATONIC_MIDI,
  PRESET_EXERCISES,
} from './contract';

test('armHeightToMidi maps 0..1 cleanly into C4..E5 pentatonic scale', () => {
  assert.equal(armHeightToMidi(0.0), 60);
  assert.equal(midiToNoteName(armHeightToMidi(0.0)), 'C4');
  assert.equal(armHeightToMidi(0.51), 69); // A4
  assert.equal(armHeightToMidi(1.0), 76);  // E5
  for (let h = 0; h <= 1.0; h += 0.05) {
    const midi = armHeightToMidi(h);
    assert.ok(PENTATONIC_MIDI.includes(midi as (typeof PENTATONIC_MIDI)[number]));
  }
});

test('knobToTolerance maps 0..100 linearly between 0.06 and 0.28', () => {
  assert.equal(knobToTolerance(0), 0.06);
  assert.equal(knobToTolerance(50), 0.17);
  assert.equal(knobToTolerance(100), 0.28);
});

test('ImuStrikeDetector rejects <95ms mechanical bounce and computes 100 BPM tempo', () => {
  const imu = new ImuStrikeDetector();
  const planned = [
    { ms: 300, amp: 0.9 },
    { ms: 340, amp: 0.7 }, // Bounce 40ms later -> must be ignored
    { ms: 900, amp: 1.4 },
    { ms: 1500, amp: 1.8 },
  ];

  const strikes: number[] = [];
  let latestBpm = 0;

  for (let t = 0; t <= 1700; t += 5) {
    let az = 1.0;
    for (const p of planned) {
      const dist = Math.abs(t - p.ms);
      if (dist <= 25) {
        az += p.amp * Math.cos((dist / 25) * (Math.PI / 2));
      }
    }
    const out = imu.stepSample(t, 0, 0, az);
    if (out.strikeIntensity !== undefined) strikes.push(out.strikeIntensity);
    if (out.bpm !== undefined) latestBpm = out.bpm;
  }

  assert.equal(strikes.length, 3);
  assert.ok(latestBpm >= 96 && latestBpm <= 104, `Expected ~100 BPM, got ${latestBpm}`);
});

test('BoardCoordinator falls back to wrist-only pentatonic arpeggio when camera/person absent', () => {
  const board = new BoardCoordinator();
  board.tick(0.066, { left: 0.2, right: 0.2, personPresent: false }, 1000);
  const n1 = board.mcuStrike(0.7, 1010);
  const n2 = board.mcuStrike(0.7, 1500);
  assert.equal(n1[0].note, 60); // C4
  assert.equal(n2[0].note, 64); // E4
});

test('BoardCoordinator completes full guided exercise session and supports custom step recording', () => {
  let completedSummarySteps = 0;
  let melodiesPlayed: string[] = [];

  const board = new BoardCoordinator({
    onTargetMelody: (m) => melodiesPlayed.push(m.kind),
    onSessionDone: (s) => {
      completedSummarySteps = s.steps_completed;
    },
  });

  // Record 2 custom steps as a physiotherapist
  board.tick(0.066, { left: 0.65, right: 0.25, personPresent: true }, 100);
  board.browserRecordStep(1.0, 120);
  board.tick(0.066, { left: 0.25, right: 0.65, personPresent: true }, 200);
  board.browserRecordStep(1.0, 220);
  assert.equal(board.customSteps.length, 2);

  // Run preset Morning Sun Reach session to completion
  board.browserStartSession('morning_sun', 1000);
  const steps = PRESET_EXERCISES[0].steps;
  let clockMs = 1000;

  for (const st of steps) {
    const ticks = Math.ceil(st.hold_sec / 0.1) + 1;
    for (let i = 0; i < ticks; i++) {
      clockMs += 100;
      board.tick(0.1, { left: st.left, right: st.right, personPresent: true }, clockMs);
    }
  }

  assert.equal(completedSummarySteps, steps.length);
  assert.equal(board.pixels, 'celebrate');
  assert.ok(melodiesPlayed.includes('step_reward'));
  assert.ok(melodiesPlayed.includes('session_complete'));
});
