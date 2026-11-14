import React from 'react';
import { PixelsPattern } from '../engine/contract';

interface ModulinoStripProps {
  pixels: PixelsPattern;
  knobValue: number;
  tolerance: number;
  bpm: number;
  lastLatencyMs: number | null;
  onKnobChange: (val: number) => void;
  onButtonPress: (btnId: 0 | 1 | 2) => void;
  onWristStrike: (intensity: number) => void;
}

function getLedColor(pattern: PixelsPattern, index: number): string {
  if (pattern === 'free_pulse') {
    return '#F59E0B'; // Warm golden amber
  }
  if (pattern === 'celebrate') {
    return index % 2 === 0 ? '#F59E0B' : '#38BDF8'; // Gold & sky shimmer
  }
  if (pattern.startsWith('progress_')) {
    const lit = parseInt(pattern.slice(9), 10) || 0;
    return index < lit ? '#10B981' : '#E7E5E4'; // Soothing emerald vs soft warm stone
  }
  // idle: center 2 LEDs gently glowing seafoam teal
  return index === 3 || index === 4 ? '#14B8A6' : '#E7E5E4';
}

export const ModulinoStrip: React.FC<ModulinoStripProps> = ({
  pixels,
  knobValue,
  tolerance,
  bpm,
  lastLatencyMs,
  onKnobChange,
  onButtonPress,
  onWristStrike,
}) => {
  return (
    <section
      aria-label="Arduino Modulino Qwiic Hardware Chain"
      className="mt-5 rounded-xl border border-stone-200 bg-white p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-4 pb-4 border-b border-stone-200">
        <div>
          <h3 className="text-sm font-semibold text-stone-900">
            Modulino Qwiic Chain (STM32 Wire1)
          </h3>
          <p className="text-xs text-stone-500 mt-0.5 font-mono tabular-nums">
            Pattern: {pixels} · Tempo: {bpm} BPM · Strike-to-Sound:{' '}
            {lastLatencyMs !== null ? `${lastLatencyMs.toFixed(1)} ms` : 'Ready (< 100 ms target)'}
          </p>
        </div>

        {/* 8-LED Modulino Pixels visualization */}
        <div
          className="flex items-center gap-2.5 bg-[#F5F2EB] px-4 py-2.5 rounded-lg border border-stone-200"
          role="group"
          aria-label={`Modulino Pixels 8-LED display (${pixels})`}
        >
          {Array.from({ length: 8 }).map((_, idx) => {
            const bg = getLedColor(pixels, idx);
            const isLit = bg !== '#E7E5E4';
            return (
              <span
                key={idx}
                style={{ backgroundColor: bg }}
                className={`block w-4 h-4 rounded-full border border-stone-300/60 transition-transform duration-150 ${
                  isLit ? 'scale-110' : 'scale-100'
                }`}
              />
            );
          })}
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 pt-4 items-center">
        {/* 1. Modulino Movement (IMU Wrist Strike) */}
        <div className="xl:col-span-4 flex items-center gap-2 min-w-0">
          <button
            type="button"
            onClick={() => onWristStrike(0.76)}
            className="flex-1 py-2.5 px-3.5 rounded-lg bg-teal-700 hover:bg-teal-800 active:scale-[0.99] text-white text-sm font-semibold transition-all whitespace-nowrap cursor-pointer"
          >
            Wrist Strike (Space)
          </button>
          <button
            type="button"
            onClick={() => onWristStrike(0.38)}
            aria-label="Soft Wrist Tap"
            className="py-2.5 px-3 rounded-lg border border-stone-200 bg-[#FAF8F5] hover:bg-stone-100 text-stone-700 text-xs font-medium transition-colors whitespace-nowrap shrink-0 cursor-pointer"
          >
            Soft Tap
          </button>
        </div>

        {/* 2. Modulino Buttons (A, B, C) */}
        <div
          className="xl:col-span-5 grid grid-cols-3 gap-2 min-w-0"
          role="group"
          aria-label="Modulino Buttons A B C"
        >
          <button
            type="button"
            onClick={() => onButtonPress(0)}
            className="py-2 px-2 rounded-lg border border-stone-200 bg-[#FAF8F5] hover:bg-stone-100 text-xs font-semibold text-stone-800 transition-colors whitespace-nowrap truncate cursor-pointer"
          >
            Btn A · Mode
          </button>
          <button
            type="button"
            onClick={() => onButtonPress(1)}
            className="py-2 px-2 rounded-lg border border-stone-200 bg-[#FAF8F5] hover:bg-stone-100 text-xs font-semibold text-stone-800 transition-colors whitespace-nowrap truncate cursor-pointer"
          >
            Btn B · Next
          </button>
          <button
            type="button"
            onClick={() => onButtonPress(2)}
            className="py-2 px-2 rounded-lg border border-stone-200 bg-[#FAF8F5] hover:bg-stone-100 text-xs font-semibold text-stone-800 transition-colors whitespace-nowrap truncate cursor-pointer"
          >
            Btn C · Save
          </button>
        </div>

        {/* 3. Modulino Knob (Tolerance 0..100) */}
        <div className="xl:col-span-3 min-w-0">
          <label
            htmlFor="modulino-knob-slider"
            className="flex items-center justify-between gap-2 text-xs font-medium text-stone-600 mb-1 font-mono tabular-nums"
          >
            <span className="truncate">Knob Tolerance</span>
            <span className="shrink-0">±{tolerance.toFixed(2)}</span>
          </label>
          <input
            id="modulino-knob-slider"
            type="range"
            min={0}
            max={100}
            value={knobValue}
            onChange={(e) => onKnobChange(Number(e.target.value))}
            className="w-full accent-teal-700 cursor-pointer"
          />
        </div>
      </div>
    </section>
  );
};
