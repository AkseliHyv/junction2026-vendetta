import React, { useState } from 'react';
import { ContractWireLog, PRESET_EXERCISES } from '../engine/contract';

interface SwarmInspectorViewProps {
  activeSubTab: 'exercises' | 'protocol' | 'checklist';
  wireLogs: ContractWireLog[];
  onStartPresetFromCard: (exerciseId: string) => void;
}

interface WorkstreamSpec {
  index: string;
  title: string;
  files: string;
  responsibility: string;
  verificationCmd: string;
}

const WORKSTREAMS: WorkstreamSpec[] = [
  {
    index: '00',
    title: 'Shared Contract Specification',
    files: 'CONTRACT.md',
    responsibility:
      'Defines exact RPC & Socket.IO message names, directions, units, rates, C4–E5 pentatonic scale table, and < 100 ms wrist-onset timing rules.',
    verificationCmd: 'Verified against sketch.ino, main.py, and index.html',
  },
  {
    index: '01',
    title: 'STM32 Modulino Firmware',
    files: 'sketch/sketch.ino · sketch/sim_imu_replay.py',
    responsibility:
      '200 Hz LSM6DSOX IMU polling on Wire1, gravity EMA filter, 95 ms strike debounce, 4-interval median BPM tempo, Buttons A/B/C, Knob 0..100, and non-red 8-LED Pixels.',
    verificationCmd: 'python3 sketch/sim_imu_replay.py',
  },
  {
    index: '02',
    title: 'RAM-Only Seated Pose Estimator',
    files: 'python/pose.py',
    responsibility:
      '320x240 USB camera capture + MediaPipe Lite upper-body landmarks, shoulder-normalized seated arm height (0..1), EMA smoothing, zero disk writes.',
    verificationCmd: 'python3 python/pose.py --fake --steps 10',
  },
  {
    index: '03',
    title: 'Pre-Warmed Web Audio Synthesizer',
    files: 'assets/synth.js · assets/synth-test.html',
    responsibility:
      'Zero-CDN Web Audio polyphonic voice pool (Left arm marimba + Right arm celesta bell), master compressor, step reward phrase, and celebratory session melody.',
    verificationCmd: 'Open /assets/synth-test.html',
  },
  {
    index: '04',
    title: 'Linux Coordinator & Seated Exercises',
    files: 'python/main.py · python/exercises.py',
    responsibility:
      'Wires Arduino_RouterBridge and WebUI Socket.IO brick, owns Free/Instruct state, gentle wobble-tolerant hold matching, custom pose recording, and wrist-only fallback.',
    verificationCmd: 'python3 python/main.py --self-test',
  },
  {
    index: '05',
    title: 'Plain HTML/JS App Lab Screen & Docs',
    files: 'assets/index.html · README.md · PROJECT_HUB.md',
    responsibility:
      '2-metre legible stick figure + ghost target canvas, automatic reconnect, offline no-board simulator, first-person-plural docs, and hardware verification list.',
    verificationCmd: 'Open /assets/index.html',
  },
];

const INITIAL_BOARD_CHECKS = [
  {
    id: 1,
    title: 'Measure Cortex-A53 Pose FPS and Camera Model on Debian Linux',
    detail:
      'Run `python3 python/pose.py --webcam 0 --steps 100` on the UNO Q terminal to fill in steady-state FPS at 320x240 (MediaPipe Lite) and the exact UVC camera model.',
    done: false,
  },
  {
    id: 2,
    title: 'Confirm `Bridge.provide("set_pixels", ...)` String Signature on Zephyr Core',
    detail:
      'Verify in Arduino App Lab compile output whether `Arduino_RouterBridge` deserializes RPC strings into Arduino `String` or `std::string` (`TODO(BOARD-CHECK)` in `sketch/sketch.ino`).',
    done: false,
  },
  {
    id: 3,
    title: 'Calibrate Wrist Strap Strike Threshold (`0.42g`) with Care-Home Residents',
    detail:
      'Test gentle seated wrist flicks with the Velcro wrist strap to ensure tremor-safe strikes trigger cleanly without double-bouncing past the 95 ms window.',
    done: false,
  },
  {
    id: 4,
    title: 'Verify Qwiic (`Wire1`) I2C Signal Integrity Across Wrist Cable Length',
    detail:
      'Confirm an 80–100 cm flexible Qwiic cable from table-mounted UNO Q (`Wire1`) to the wrist-worn `ModulinoMovement` runs cleanly during full overhead arm reach.',
    done: false,
  },
  {
    id: 5,
    title: 'Log Physical Strike-to-Sound Wi-Fi Latency (`< 100 ms` Target)',
    detail:
      'Connect a laptop/tablet over local Wi-Fi to `http://<uno-q-ip>:7000`, tap the wrist IMU, and inspect `[Latency]` terminal logs in `python/main.py`.',
    done: false,
  },
  {
    id: 6,
    title: 'Validate 2-Metre Screen & Modulino Pixels Brightness in Clinic Lighting',
    detail:
      'Check stick-figure contrast on `assets/index.html` from a chair 2 metres away and confirm `DEFAULT_BRIGHTNESS = 18` on `ModulinoPixels` is comfortable indoors.',
    done: false,
  },
];

export const SwarmInspectorView: React.FC<SwarmInspectorViewProps> = ({
  activeSubTab,
  wireLogs,
  onStartPresetFromCard,
}) => {
  const [checks, setChecks] = useState(INITIAL_BOARD_CHECKS);

  const toggleCheck = (id: number) => {
    setChecks((prev) => prev.map((c) => (c.id === id ? { ...c, done: !c.done } : c)));
  };

  if (activeSubTab === 'exercises') {
    return (
      <section className="space-y-8" aria-label="Seated Rehabilitation Exercise Library">
        <div className="max-w-2xl">
          <h1 className="text-3xl font-semibold tracking-tight text-stone-900">
            Seated Rehabilitation Routines
          </h1>
          <p className="mt-2 text-base text-stone-600 leading-relaxed">
            Four gentle, chair-safe movement progressions shipped in{' '}
            <code className="font-mono text-sm text-stone-800">python/exercises.py</code>.
            Unreached poses wait patiently without red warnings while every wrist motion continues making music.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {PRESET_EXERCISES.map((ex, idx) => (
            <article
              key={ex.id}
              className="rounded-xl border border-stone-200 bg-white p-6 flex flex-col justify-between gap-5"
            >
              <div>
                <div className="text-xs text-stone-500 font-mono tabular-nums mb-1.5">
                  0{idx + 1}. {ex.steps.length} steps · Seated accessible
                </div>
                <h2 className="text-xl font-semibold text-stone-900">{ex.name}</h2>
                <p className="text-sm text-stone-600 mt-1">{ex.clinicalFocus}</p>

                <ol className="mt-4 space-y-2 border-t border-stone-100 pt-4">
                  {ex.steps.map((st, sIdx) => (
                    <li
                      key={sIdx}
                      className="flex items-baseline justify-between gap-4 text-sm text-stone-700"
                    >
                      <span>
                        <span className="font-mono text-xs text-stone-400 mr-2">
                          {sIdx + 1}.
                        </span>
                        {st.instruction}
                      </span>
                      <span className="font-mono text-xs text-stone-500 tabular-nums shrink-0">
                        L:{st.left.toFixed(2)} R:{st.right.toFixed(2)} · {st.hold_sec.toFixed(1)}s
                      </span>
                    </li>
                  ))}
                </ol>
              </div>

              <div className="pt-2">
                <button
                  type="button"
                  onClick={() => onStartPresetFromCard(ex.id)}
                  className="px-4 py-2.5 rounded-lg bg-teal-700 hover:bg-teal-800 text-white text-sm font-semibold transition-colors whitespace-nowrap cursor-pointer"
                >
                  Launch {ex.name} on Stage
                </button>
              </div>
            </article>
          ))}
        </div>
      </section>
    );
  }

  if (activeSubTab === 'checklist') {
    const completedCount = checks.filter((c) => c.done).length;
    return (
      <section className="space-y-8" aria-label="Physical UNO Q Board Verification Checklist">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="max-w-2xl">
            <h1 className="text-3xl font-semibold tracking-tight text-stone-900">
              Physical UNO Q Board Checklist
            </h1>
            <p className="mt-2 text-base text-stone-600 leading-relaxed">
              Every module runs standalone on a laptop with simulated inputs. Below is the exact numbered list of hardware checks remaining once flashed onto the Arduino UNO Q.
            </p>
          </div>
          <div className="text-sm font-mono tabular-nums text-stone-600">
            {completedCount} of {checks.length} verified on hardware
          </div>
        </div>

        <div className="rounded-xl border border-stone-200 bg-white divide-y divide-stone-200">
          {checks.map((item) => (
            <label
              key={item.id}
              className="flex items-start gap-4 p-5 hover:bg-[#FAF8F5] transition-colors cursor-pointer"
            >
              <input
                type="checkbox"
                checked={item.done}
                onChange={() => toggleCheck(item.id)}
                className="mt-1 h-5 w-5 rounded accent-teal-700 cursor-pointer shrink-0"
              />
              <div className="space-y-1">
                <div className="text-base font-semibold text-stone-900">
                  <span className="font-mono text-sm text-teal-800 mr-2">0{item.id}.</span>
                  {item.title}
                </div>
                <p className="text-sm text-stone-600 leading-relaxed">{item.detail}</p>
              </div>
            </label>
          ))}
        </div>
      </section>
    );
  }

  return (
    <section className="space-y-8" aria-label="Swarm Workstreams and Live CONTRACT.md Protocol">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-2xl">
          <h1 className="text-3xl font-semibold tracking-tight text-stone-900">
            UNO Q App Lab Workstreams &amp; Live Contract Bus
          </h1>
          <p className="mt-2 text-base text-stone-600 leading-relaxed">
            Strict 6-workstream separation governed by{' '}
            <code className="font-mono text-sm text-stone-800">CONTRACT.md</code>. Open the plain HTML/JS App Lab artifacts directly to test zero-framework browser execution.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <a
            href="/assets/index.html"
            target="_blank"
            rel="noreferrer"
            className="px-4 py-2 rounded-lg bg-stone-900 hover:bg-stone-800 text-white text-sm font-semibold transition-colors whitespace-nowrap"
          >
            Open Standalone assets/index.html
          </a>
          <a
            href="/assets/synth-test.html"
            target="_blank"
            rel="noreferrer"
            className="px-4 py-2 rounded-lg border border-stone-200 bg-white hover:bg-stone-100 text-stone-800 text-sm font-semibold transition-colors whitespace-nowrap"
          >
            Open Keyboard Synth Test
          </a>
        </div>
      </div>

      {/* 6 Workstream Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        {WORKSTREAMS.map((ws) => (
          <article
            key={ws.index}
            className="rounded-xl border border-stone-200 bg-white p-5 flex flex-col justify-between gap-4"
          >
            <div>
              <div className="text-xs font-mono text-teal-800 mb-1">
                {ws.index}. {ws.files}
              </div>
              <h2 className="text-lg font-semibold text-stone-900">{ws.title}</h2>
              <p className="text-sm text-stone-600 mt-1.5 leading-relaxed">
                {ws.responsibility}
              </p>
            </div>
            <div className="pt-3 border-t border-stone-100 text-xs font-mono text-stone-500">
              Check: {ws.verificationCmd}
            </div>
          </article>
        ))}
      </div>

      {/* Live CONTRACT.md Wire Inspector */}
      <div className="rounded-xl border border-stone-200 bg-white p-6">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold text-stone-900">
              Live CONTRACT.md Message Bus
            </h2>
            <p className="text-xs text-stone-500 mt-0.5">
              Real-time events across STM32 Bridge and Linux WebUI channels (excluding 15 Hz steady state ticks)
            </p>
          </div>
          <span className="text-xs font-mono text-stone-500 tabular-nums">
            {wireLogs.length} recent events
          </span>
        </div>

        {wireLogs.length === 0 ? (
          <p className="text-sm text-stone-500 py-6 text-center bg-[#FAF8F5] rounded-lg">
            Trigger a wrist strike or switch modes on the Instrument Stage to inspect live protocol frames.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-sm">
              <thead>
                <tr className="border-b border-stone-200 text-xs text-stone-500">
                  <th className="py-2 pr-4 font-medium">Timestamp (ms)</th>
                  <th className="py-2 pr-4 font-medium">Direction</th>
                  <th className="py-2 pr-4 font-medium">Message</th>
                  <th className="py-2 font-medium">Payload Summary</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100 font-mono text-xs tabular-nums">
                {wireLogs.map((entry) => (
                  <tr key={entry.id}>
                    <td className="py-2 pr-4 text-stone-500">{entry.ts_ms}</td>
                    <td className="py-2 pr-4 text-stone-700">{entry.channel}</td>
                    <td className="py-2 pr-4 font-semibold text-teal-800">{entry.name}</td>
                    <td className="py-2 text-stone-800">{entry.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
};
