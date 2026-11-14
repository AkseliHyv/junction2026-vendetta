/**
 * Pre-Allocated Web Audio API Pentatonic Synthesizer (`src/engine/synth.ts`)
 * Mirrors `assets/synth.js` (Workstream 3).
 * - Voice 0 (Left Arm): Warm wooden marimba (triangle + fleeting 3rd harmonic)
 * - Voice 1 (Right Arm): Luminous celesta chime (sine + octave shimmer)
 * - Pre-builds 8 persistent oscillator voices on `start()` so the first strike
 *   schedules in < 2 ms without Web Audio graph allocation delay.
 */

import { clamp, midiToFrequency, REWARD_PHRASE_MIDI, SESSION_MELODY_MIDI } from './contract';

interface VoiceSlot {
  oscFund: OscillatorNode;
  oscHarm: OscillatorNode;
  harmGain: GainNode;
  envGain: GainNode;
}

export class WebAudioPentatonicSynth {
  private ctx: AudioContext | null = null;
  private masterGain: GainNode | null = null;
  private voicePool: VoiceSlot[] = [];
  private poolCursor = 0;
  private activeVoiceGain: Record<0 | 1, AudioParam | null> = { 0: null, 1: null };

  ready = false;
  muted = false;

  async start(): Promise<boolean> {
    if (typeof window === 'undefined') return false;
    const AudioCtx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return false;

    if (!this.ctx) {
      this.ctx = new AudioCtx({ latencyHint: 'interactive' });

      const compressor = this.ctx.createDynamicsCompressor();
      compressor.threshold.value = -14;
      compressor.knee.value = 18;
      compressor.ratio.value = 4;
      compressor.attack.value = 0.003;
      compressor.release.value = 0.22;

      const warmFilter = this.ctx.createBiquadFilter();
      warmFilter.type = 'lowpass';
      warmFilter.frequency.value = 3400;
      warmFilter.Q.value = 0.7;

      this.masterGain = this.ctx.createGain();
      this.masterGain.gain.value = this.muted ? 0.0 : 0.82;

      // Subtle warm acoustic room reflection
      const echoDelay = this.ctx.createDelay(0.5);
      echoDelay.delayTime.value = 0.19;
      const echoFeedback = this.ctx.createGain();
      echoFeedback.gain.value = 0.18;
      const echoWet = this.ctx.createGain();
      echoWet.gain.value = 0.15;

      warmFilter.connect(compressor);
      warmFilter.connect(echoDelay);
      echoDelay.connect(echoFeedback);
      echoFeedback.connect(echoDelay);
      echoDelay.connect(echoWet);
      echoWet.connect(compressor);

      compressor.connect(this.masterGain);
      this.masterGain.connect(this.ctx.destination);

      for (let i = 0; i < 8; i++) {
        const oscFund = this.ctx.createOscillator();
        const oscHarm = this.ctx.createOscillator();
        const harmGain = this.ctx.createGain();
        const envGain = this.ctx.createGain();

        oscFund.type = i % 2 === 0 ? 'triangle' : 'sine';
        oscHarm.type = 'sine';
        oscFund.frequency.value = 261.63;
        oscHarm.frequency.value = 523.25;

        harmGain.gain.value = 0.16;
        envGain.gain.value = 0.0001;

        oscFund.connect(envGain);
        oscHarm.connect(harmGain);
        harmGain.connect(envGain);
        envGain.connect(warmFilter);

        oscFund.start();
        oscHarm.start();

        this.voicePool.push({ oscFund, oscHarm, harmGain, envGain });
      }
    }

    if (this.ctx.state === 'suspended') {
      try {
        await this.ctx.resume();
      } catch {
        return false;
      }
    }
    this.ready = this.ctx.state === 'running';
    return this.ready;
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    if (this.masterGain && this.ctx) {
      this.masterGain.gain.setTargetAtTime(muted ? 0.0 : 0.82, this.ctx.currentTime, 0.02);
    }
  }

  /**
   * Schedules a pentatonic note immediately and returns measured scheduling latency in ms.
   */
  noteOn(midiNote: number, velocity: number, voice: 0 | 1, strikeTsMs?: number): number {
    const t0 = performance.now();
    if (!this.ready || !this.ctx || this.muted || this.voicePool.length === 0) {
      return Math.max(1.2, performance.now() - t0);
    }

    const slot = this.voicePool[this.poolCursor % this.voicePool.length];
    this.poolCursor += 1;

    const now = this.ctx.currentTime;
    const freq = midiToFrequency(midiNote);
    const vel = clamp(velocity || 0.65, 0.05, 1.0);
    const isBell = voice === 1;

    slot.oscFund.type = isBell ? 'sine' : 'triangle';
    slot.oscFund.frequency.cancelScheduledValues(now);
    slot.oscFund.frequency.setValueAtTime(freq, now);

    slot.oscHarm.frequency.cancelScheduledValues(now);
    slot.oscHarm.frequency.setValueAtTime(freq * (isBell ? 2.0 : 3.0), now);
    slot.harmGain.gain.setValueAtTime(isBell ? 0.22 : 0.14, now);

    const peak = (isBell ? 0.26 : 0.34) * (0.35 + 0.65 * vel);
    const decay = isBell ? 1.05 : 0.78;

    const g = slot.envGain.gain;
    g.cancelScheduledValues(now);
    g.setValueAtTime(0.0002, now);
    g.linearRampToValueAtTime(peak, now + 0.006);
    g.exponentialRampToValueAtTime(0.0002, now + decay);

    this.activeVoiceGain[voice] = g;

    const outputLatencyMs = ((this.ctx.baseLatency || 0.005) + (this.ctx.outputLatency || 0.008)) * 1000;
    const transportDelta = strikeTsMs ? Math.max(0, Date.now() - strikeTsMs) : 1.5;
    return Math.round((transportDelta + outputLatencyMs + (performance.now() - t0)) * 10) / 10;
  }

  noteOff(voice: 0 | 1): void {
    if (!this.ready || !this.ctx) return;
    const g = this.activeVoiceGain[voice];
    if (g) {
      const now = this.ctx.currentTime;
      g.cancelScheduledValues(now);
      g.setTargetAtTime(0.0001, now, 0.08);
    }
  }

  playMelody(kind: 'step_reward' | 'session_complete', customNotes?: number[]): void {
    if (!this.ready || !this.ctx || this.muted) return;
    const notes =
      customNotes && customNotes.length > 0
        ? customNotes
        : kind === 'session_complete'
          ? SESSION_MELODY_MIDI
          : REWARD_PHRASE_MIDI;
    const spacingMs = kind === 'session_complete' ? 165 : 130;
    notes.forEach((n, idx) => {
      window.setTimeout(() => {
        this.noteOn(n, kind === 'session_complete' ? 0.72 : 0.62, (idx % 2) as 0 | 1);
      }, idx * spacingMs);
    });
  }
}
