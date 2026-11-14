import React from 'react';
import {
  armHeightToMidi,
  ExerciseStep,
  InstrumentMode,
  midiToNoteName,
  PRESET_EXERCISES,
  StepMessage,
} from '../engine/contract';
import { CameraDeviceOption, CameraFeedStatus } from '../engine/webcamMotion';

export type PoseInputSource = 'webcam' | 'manual' | 'auto_patient';

interface PhysioPanelProps {
  mode: InstrumentMode;
  exerciseId: string;
  customSteps: ExerciseStep[];
  inputSource: PoseInputSource;
  cameraStatus: CameraFeedStatus;
  cameraDevices: CameraDeviceOption[];
  selectedCameraId: string;
  mirrorVideo: boolean;
  opticalFlickEnabled: boolean;
  webcamError: string | null;
  left: number;
  right: number;
  personPresent: boolean;
  autoPulse: boolean;
  currentStep: StepMessage | null;
  onSetMode: (mode: InstrumentMode) => void;
  onSelectExercise: (id: string) => void;
  onStartSession: (id: string) => void;
  onRecordCustomStep: () => void;
  onClearCustomSteps: () => void;
  onChangeInputSource: (src: PoseInputSource) => void;
  onSelectCameraDevice: (deviceId: string) => void;
  onToggleMirrorVideo: (mirror: boolean) => void;
  onToggleOpticalFlick: (enabled: boolean) => void;
  onStartSyntheticCamera: () => void;
  onSetArmLevels: (left: number, right: number) => void;
  onTogglePersonPresent: (present: boolean) => void;
  onToggleAutoPulse: (enabled: boolean) => void;
  onMatchTargetPose: () => void;
}

export const PhysioPanel: React.FC<PhysioPanelProps> = ({
  mode,
  exerciseId,
  customSteps,
  inputSource,
  cameraStatus,
  cameraDevices,
  selectedCameraId,
  mirrorVideo,
  opticalFlickEnabled,
  webcamError,
  left,
  right,
  personPresent,
  autoPulse,
  currentStep,
  onSetMode,
  onSelectExercise,
  onStartSession,
  onRecordCustomStep,
  onClearCustomSteps,
  onChangeInputSource,
  onSelectCameraDevice,
  onToggleMirrorVideo,
  onToggleOpticalFlick,
  onStartSyntheticCamera,
  onSetArmLevels,
  onTogglePersonPresent,
  onToggleAutoPulse,
  onMatchTargetPose,
}) => {
  const leftNote = midiToNoteName(armHeightToMidi(left));
  const rightNote = midiToNoteName(armHeightToMidi(right));

  return (
    <aside
      aria-label="Physiotherapist and Camera Control Panel"
      className="rounded-xl border border-stone-200 bg-white p-6 flex flex-col gap-5"
    >
      {/* 1. Mode Switch */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-base font-semibold text-stone-900 font-sans">
            Session Mode
          </h2>
          <span className="text-xs text-stone-500">Board-owned state</span>
        </div>
        <div
          role="group"
          aria-label="Select Instrument Mode"
          className="grid grid-cols-2 gap-1 p-1 rounded-lg bg-[#F5F2EB] border border-stone-200"
        >
          <button
            type="button"
            onClick={() => onSetMode('free')}
            className={`py-2 px-3 rounded-md text-sm font-semibold transition-colors whitespace-nowrap cursor-pointer ${
              mode === 'free'
                ? 'bg-white text-stone-900 shadow-xs'
                : 'text-stone-600 hover:text-stone-900'
            }`}
          >
            Free Instrument
          </button>
          <button
            type="button"
            onClick={() => onSetMode('instruct')}
            className={`py-2 px-3 rounded-md text-sm font-semibold transition-colors whitespace-nowrap cursor-pointer ${
              mode === 'instruct'
                ? 'bg-white text-stone-900 shadow-xs'
                : 'text-stone-600 hover:text-stone-900'
            }`}
          >
            Guided Exercise
          </button>
        </div>
      </div>

      {/* 2. Physiotherapist Exercise & Custom Step Controls */}
      <div className="pt-4 border-t border-stone-200">
        <h2 className="text-base font-semibold text-stone-900 font-sans mb-2.5">
          Physiotherapist Routine
        </h2>

        <label htmlFor="physio-exercise-select" className="block text-xs font-medium text-stone-600 mb-1.5">
          Seated Exercise Preset
        </label>
        <select
          id="physio-exercise-select"
          value={exerciseId}
          onChange={(e) => onSelectExercise(e.target.value)}
          className="w-full rounded-lg border border-stone-200 bg-[#FAF8F5] px-3 py-2 text-sm font-medium text-stone-900 focus:outline-none focus:ring-2 focus:ring-teal-700"
        >
          {PRESET_EXERCISES.map((ex) => (
            <option key={ex.id} value={ex.id}>
              {ex.name} ({ex.steps.length} steps)
            </option>
          ))}
          <option value="custom">
            Custom Physio Routine ({customSteps.length} {customSteps.length === 1 ? 'step' : 'steps'})
          </option>
        </select>

        <div className="grid grid-cols-2 gap-2 mt-3">
          <button
            type="button"
            onClick={() => onStartSession(exerciseId)}
            className="py-2 px-3 rounded-lg bg-stone-900 hover:bg-stone-800 text-white text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer"
          >
            Start Guided Session
          </button>
          {mode === 'instruct' && currentStep ? (
            <button
              type="button"
              onClick={onMatchTargetPose}
              className="py-2 px-3 rounded-lg border border-teal-700 bg-teal-50 hover:bg-teal-100 text-teal-900 text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer"
            >
              Match Target Pose
            </button>
          ) : (
            <button
              type="button"
              onClick={onRecordCustomStep}
              className="py-2 px-3 rounded-lg border border-stone-200 bg-[#FAF8F5] hover:bg-stone-100 text-stone-800 text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer"
            >
              Record Custom Step
            </button>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 mt-2">
          <button
            type="button"
            onClick={onRecordCustomStep}
            className="text-xs font-medium text-teal-800 hover:underline cursor-pointer whitespace-nowrap"
          >
            + Capture current pose ({customSteps.length} saved)
          </button>
          {customSteps.length > 0 && (
            <button
              type="button"
              onClick={onClearCustomSteps}
              className="text-xs font-medium text-stone-500 hover:text-stone-900 hover:underline cursor-pointer whitespace-nowrap"
            >
              Clear custom
            </button>
          )}
        </div>
      </div>

      {/* 3. Live Arm Levels & Manual Calibration Sliders */}
      <div className="pt-4 border-t border-stone-200">
        <div className="flex items-center justify-between mb-2.5">
          <h2 className="text-base font-semibold text-stone-900 font-sans">
            Live Arm Pitch Levels
          </h2>
          <span className="text-xs text-stone-500 font-mono tabular-nums">
            0.00 – 1.00
          </span>
        </div>

        <div className="grid grid-cols-2 gap-3 mb-3.5">
          <div className="rounded-lg bg-[#F5F2EB] p-2.5 border border-stone-200/80">
            <div className="text-xs text-stone-600">Left Arm · Marimba</div>
            <div className="text-lg font-bold text-amber-700 font-mono tabular-nums mt-0.5">
              {left.toFixed(2)} · {leftNote}
            </div>
          </div>
          <div className="rounded-lg bg-[#F5F2EB] p-2.5 border border-stone-200/80">
            <div className="text-xs text-stone-600">Right Arm · Celesta</div>
            <div className="text-lg font-bold text-sky-700 font-mono tabular-nums mt-0.5">
              {right.toFixed(2)} · {rightNote}
            </div>
          </div>
        </div>

        <div className="space-y-2.5">
          <div>
            <label
              htmlFor="left-arm-slider"
              className="flex justify-between text-xs font-medium text-stone-600 mb-0.5 font-mono tabular-nums"
            >
              <span>Left Arm Override (W / S)</span>
              <span>{left.toFixed(2)}</span>
            </label>
            <input
              id="left-arm-slider"
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={left}
              onChange={(e) => onSetArmLevels(Number(e.target.value), right)}
              className="w-full accent-amber-600 cursor-pointer"
            />
          </div>

          <div>
            <label
              htmlFor="right-arm-slider"
              className="flex justify-between text-xs font-medium text-stone-600 mb-0.5 font-mono tabular-nums"
            >
              <span>Right Arm Override (Up / Down)</span>
              <span>{right.toFixed(2)}</span>
            </label>
            <input
              id="right-arm-slider"
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={right}
              onChange={(e) => onSetArmLevels(left, Number(e.target.value))}
              className="w-full accent-sky-600 cursor-pointer"
            />
          </div>
        </div>
      </div>

      {/* 4. Real Camera Feed & Tracking Controls */}
      <div className="pt-4 border-t border-stone-200">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-base font-semibold text-stone-900 font-sans">
            Camera &amp; Tracking Source
          </h2>
          <span className="text-xs font-mono text-stone-500">
            {cameraStatus === 'live'
              ? 'Live Cam'
              : cameraStatus === 'synthetic'
                ? 'Test Stream'
                : 'Standby'}
          </span>
        </div>

        <div
          role="group"
          aria-label="Pose Input Source"
          className="grid grid-cols-3 gap-1 p-1 rounded-lg bg-[#F5F2EB] border border-stone-200 mb-3"
        >
          <button
            type="button"
            onClick={() => onChangeInputSource('webcam')}
            className={`py-1.5 px-2 rounded text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer ${
              inputSource === 'webcam'
                ? 'bg-white text-stone-900 shadow-xs'
                : 'text-stone-600 hover:text-stone-900'
            }`}
          >
            Live Camera
          </button>
          <button
            type="button"
            onClick={() => onChangeInputSource('manual')}
            className={`py-1.5 px-2 rounded text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer ${
              inputSource === 'manual'
                ? 'bg-white text-stone-900 shadow-xs'
                : 'text-stone-600 hover:text-stone-900'
            }`}
          >
            Manual Hold
          </button>
          <button
            type="button"
            onClick={() => onChangeInputSource('auto_patient')}
            className={`py-1.5 px-2 rounded text-xs font-semibold transition-colors whitespace-nowrap cursor-pointer ${
              inputSource === 'auto_patient'
                ? 'bg-white text-stone-900 shadow-xs'
                : 'text-stone-600 hover:text-stone-900'
            }`}
          >
            Auto Patient
          </button>
        </div>

        {cameraDevices.length > 1 && (
          <div className="mb-3">
            <label htmlFor="camera-device-select" className="block text-xs font-medium text-stone-600 mb-1">
              Video Input Device
            </label>
            <select
              id="camera-device-select"
              value={selectedCameraId}
              onChange={(e) => onSelectCameraDevice(e.target.value)}
              className="w-full rounded-lg border border-stone-200 bg-[#FAF8F5] px-2.5 py-1.5 text-xs text-stone-900"
            >
              {cameraDevices.map((d) => (
                <option key={d.deviceId} value={d.deviceId}>
                  {d.label}
                </option>
              ))}
            </select>
          </div>
        )}

        {webcamError && (
          <div className="text-xs text-stone-700 bg-[#F5F2EB] rounded-lg p-2.5 mb-3 flex items-center justify-between gap-2">
            <span>{webcamError}</span>
            <button
              type="button"
              onClick={onStartSyntheticCamera}
              className="px-2 py-1 rounded bg-stone-900 text-white font-semibold whitespace-nowrap shrink-0 cursor-pointer"
            >
              Test Feed
            </button>
          </div>
        )}

        <div className="space-y-2 text-xs text-stone-700">
          <label className="flex items-center justify-between gap-2 cursor-pointer">
            <span>Mirror Camera Feed</span>
            <input
              type="checkbox"
              checked={mirrorVideo}
              onChange={(e) => onToggleMirrorVideo(e.target.checked)}
              className="rounded accent-teal-700 w-4 h-4 cursor-pointer"
            />
          </label>
          <label className="flex items-center justify-between gap-2 cursor-pointer">
            <span>Camera Wrist-Flick Strikes</span>
            <input
              type="checkbox"
              checked={opticalFlickEnabled}
              onChange={(e) => onToggleOpticalFlick(e.target.checked)}
              className="rounded accent-teal-700 w-4 h-4 cursor-pointer"
            />
          </label>
          <label className="flex items-center justify-between gap-2 cursor-pointer">
            <span>Auto Wrist Rhythm (92 BPM)</span>
            <input
              type="checkbox"
              checked={autoPulse}
              onChange={(e) => onToggleAutoPulse(e.target.checked)}
              className="rounded accent-teal-700 w-4 h-4 cursor-pointer"
            />
          </label>
          <label className="flex items-center justify-between gap-2 cursor-pointer">
            <span>Person Present (uncheck for arpeggio fallback)</span>
            <input
              type="checkbox"
              checked={personPresent}
              onChange={(e) => onTogglePersonPresent(e.target.checked)}
              className="rounded accent-teal-700 w-4 h-4 cursor-pointer"
            />
          </label>
        </div>
      </div>
    </aside>
  );
};
