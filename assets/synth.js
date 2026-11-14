/**
 * Workstream 3: Browser Web Audio Synthesizer (assets/synth.js)
 * -------------------------------------------------------------
 * Zero-dependency Web Audio API synthesizer for care homes & rehab clinics.
 * - Pre-creates master dynamics compressor, warmth low-pass filter, gentle
 *   delay bus, and an 8-slot voice pool on `start()` so the VERY FIRST note
 *   after clicking "Start Audio" schedules in < 2 ms without late allocation.
 * - Voice 0 (Left Arm): Warm wooden marimba / kalimba (triangle + 2nd harmonic)
 * - Voice 1 (Right Arm): Luminous celesta / chime bell (sine + soft overtone)
 * - `playMelody("step_reward" | "session_complete", notes)` for Instruct mode.
 *
 * Exposes `window.KinesthesiaSynth` for plain HTML (`assets/index.html` and
 * `assets/synth-test.html`). Zero external CDN links.
 */
(function (global) {
  'use strict';

  var PENTATONIC_MIDI = [60, 62, 64, 67, 69, 72, 74, 76];
  var NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

  function midiToFreq(midi) {
    return 440 * Math.pow(2, (midi - 69) / 12);
  }

  function midiToName(midi) {
    var m = Math.round(midi);
    var octave = Math.floor(m / 12) - 1;
    return NOTE_NAMES[((m % 12) + 12) % 12] + octave;
  }

  function KinesthesiaSynth() {
    this.ctx = null;
    this.masterGain = null;
    this.compressor = null;
    this.warmFilter = null;
    this.echoDelay = null;
    this.echoFeedback = null;
    this.voicePool = [];
    this.poolCursor = 0;
    this.ready = false;
    this.muted = false;
    this.activeVoiceGain = { 0: null, 1: null };
  }

  /**
   * Must be invoked inside a visible user gesture ("Start Audio" click).
   * Pre-builds the entire signal chain and voice pool immediately.
   */
  KinesthesiaSynth.prototype.start = function () {
    var AudioCtx = global.AudioContext || global.webkitAudioContext;
    if (!AudioCtx) {
      return Promise.resolve(false);
    }

    if (!this.ctx) {
      this.ctx = new AudioCtx({ latencyHint: 'interactive' });

      // Master safety limiter/compressor so chords never clip
      this.compressor = this.ctx.createDynamicsCompressor();
      this.compressor.threshold.value = -14;
      this.compressor.knee.value = 18;
      this.compressor.ratio.value = 4;
      this.compressor.attack.value = 0.003;
      this.compressor.release.value = 0.22;

      this.warmFilter = this.ctx.createBiquadFilter();
      this.warmFilter.type = 'lowpass';
      this.warmFilter.frequency.value = 3400;
      this.warmFilter.Q.value = 0.7;

      this.masterGain = this.ctx.createGain();
      this.masterGain.gain.value = 0.82;

      // Subtle room ambience (acoustic warmth without muddiness)
      this.echoDelay = this.ctx.createDelay(0.5);
      this.echoDelay.delayTime.value = 0.19;
      this.echoFeedback = this.ctx.createGain();
      this.echoFeedback.gain.value = 0.18;
      var echoWet = this.ctx.createGain();
      echoWet.gain.value = 0.16;

      this.warmFilter.connect(this.compressor);
      this.warmFilter.connect(this.echoDelay);
      this.echoDelay.connect(this.echoFeedback);
      this.echoFeedback.connect(this.echoDelay);
      this.echoDelay.connect(echoWet);
      echoWet.connect(this.compressor);

      this.compressor.connect(this.masterGain);
      this.masterGain.connect(this.ctx.destination);

      // Pre-allocate persistent oscillator + envelope slots so first note has zero GC/init stall
      for (var i = 0; i < 8; i++) {
        var oscFund = this.ctx.createOscillator();
        var oscHarm = this.ctx.createOscillator();
        var harmGain = this.ctx.createGain();
        var envGain = this.ctx.createGain();

        oscFund.type = i % 2 === 0 ? 'triangle' : 'sine';
        oscHarm.type = 'sine';
        oscFund.frequency.value = 261.63;
        oscHarm.frequency.value = 523.25;

        harmGain.gain.value = 0.18;
        envGain.gain.value = 0.0001;

        oscFund.connect(envGain);
        oscHarm.connect(harmGain);
        harmGain.connect(envGain);
        envGain.connect(this.warmFilter);

        oscFund.start();
        oscHarm.start();

        this.voicePool.push({
          oscFund: oscFund,
          oscHarm: oscHarm,
          harmGain: harmGain,
          envGain: envGain
        });
      }
    }

    var self = this;
    return this.ctx.resume().then(function () {
      self.ready = true;
      return true;
    });
  };

  KinesthesiaSynth.prototype.setMuted = function (mute) {
    this.muted = !!mute;
    if (this.masterGain && this.ctx) {
      this.masterGain.gain.setTargetAtTime(this.muted ? 0.0 : 0.82, this.ctx.currentTime, 0.02);
    }
  };

  /**
   * Triggers immediate pentatonic note onset.
   * @param {number} midiNote - MIDI note (e.g. 60..76)
   * @param {number} velocity - 0.05..1.0
   * @param {number} voice - 0 (Left arm marimba) | 1 (Right arm celesta bell)
   * @returns {number} AudioContext currentTime when scheduled
   */
  KinesthesiaSynth.prototype.noteOn = function (midiNote, velocity, voice) {
    if (!this.ready || !this.ctx || this.muted) {
      return 0;
    }
    var slot = this.voicePool[this.poolCursor % this.voicePool.length];
    this.poolCursor += 1;

    var now = this.ctx.currentTime;
    var freq = midiToFreq(midiNote);
    var vel = Math.max(0.05, Math.min(1.0, Number(velocity) || 0.65));
    var isBell = Number(voice) === 1;

    slot.oscFund.type = isBell ? 'sine' : 'triangle';
    slot.oscFund.frequency.cancelScheduledValues(now);
    slot.oscFund.frequency.setValueAtTime(freq, now);

    // Marimba uses 4x harmonic fleeting strike; Bell uses 2x octave shimmer
    slot.oscHarm.frequency.cancelScheduledValues(now);
    slot.oscHarm.frequency.setValueAtTime(freq * (isBell ? 2.0 : 3.0), now);
    slot.harmGain.gain.setValueAtTime(isBell ? 0.22 : 0.14, now);

    var peak = (isBell ? 0.26 : 0.34) * (0.35 + 0.65 * vel);
    var decay = isBell ? 1.05 : 0.78;

    var g = slot.envGain.gain;
    g.cancelScheduledValues(now);
    g.setValueAtTime(0.0002, now);
    g.linearRampToValueAtTime(peak, now + 0.006);
    g.exponentialRampToValueAtTime(0.0002, now + decay);

    this.activeVoiceGain[voice === 1 ? 1 : 0] = g;
    return now;
  };

  KinesthesiaSynth.prototype.noteOff = function (voice) {
    if (!this.ready || !this.ctx) return;
    var g = this.activeVoiceGain[voice === 1 ? 1 : 0];
    if (g) {
      var now = this.ctx.currentTime;
      g.cancelScheduledValues(now);
      g.setTargetAtTime(0.0001, now, 0.08);
    }
  };

  /**
   * Plays either the gentle 3-note `step_reward` phrase or the celebratory
   * `session_complete` pentatonic melody.
   */
  KinesthesiaSynth.prototype.playMelody = function (kind, customNotes) {
    if (!this.ready || !this.ctx || this.muted) return;
    var notes = Array.isArray(customNotes) && customNotes.length
      ? customNotes
      : (kind === 'session_complete' ? [60, 64, 67, 72, 76, 72, 76] : [67, 72, 76]);
    var spacingMs = kind === 'session_complete' ? 165 : 130;
    var self = this;
    notes.forEach(function (n, idx) {
      setTimeout(function () {
        self.noteOn(n, kind === 'session_complete' ? 0.72 : 0.62, idx % 2);
      }, idx * spacingMs);
    });
  };

  KinesthesiaSynth.PENTATONIC_MIDI = PENTATONIC_MIDI;
  KinesthesiaSynth.midiToName = midiToName;
  KinesthesiaSynth.midiToFreq = midiToFreq;

  global.KinesthesiaSynth = KinesthesiaSynth;
})(typeof window !== 'undefined' ? window : globalThis);
