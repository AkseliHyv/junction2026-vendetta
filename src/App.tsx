import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  armHeightToMidi,
  BoardCoordinator,
  clamp,
  ContractWireLog,
  ExerciseStep,
  InstrumentMode,
  midiToNoteName,
  PixelsPattern,
  PRESET_EXERCISES,
  SessionSummary,
  StepMessage,
} from './engine/contract';
import { WebAudioPentatonicSynth } from './engine/synth';
import {
  CameraDeviceOption,
  CameraFeedStatus,
  PrivacySafeWebcamEstimator,
} from './engine/webcamMotion';
import { StageCanvas, WristRipple } from './components/StageCanvas';
import { ModulinoStrip } from './components/ModulinoStrip';
import { PhysioPanel, PoseInputSource } from './components/PhysioPanel';
import { SwarmInspectorView } from './components/SwarmInspectorView';

type ActiveNavTab = 'stage' | 'exercises' | 'protocol' | 'checklist';

export default function App() {
  const [activeTab, setActiveTab] = useState<ActiveNavTab>('stage');

  // Audio state
  const synthRef = useRef<WebAudioPentatonicSynth | null>(null);
  if (!synthRef.current) {
    synthRef.current = new WebAudioPentatonicSynth();
  }
  const [audioStarted, setAudioStarted] = useState(false);
  const [audioMuted, setAudioMuted] = useState(false);
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null);

  // Authoritative UNO Q BoardCoordinator & Live Camera Tracker
  const boardRef = useRef<BoardCoordinator | null>(null);
  if (!boardRef.current) {
    boardRef.current = new BoardCoordinator();
  }
  const webcamRef = useRef<PrivacySafeWebcamEstimator | null>(null);
  if (!webcamRef.current) {
    webcamRef.current = new PrivacySafeWebcamEstimator();
  }
  const rippleSeqRef = useRef(1);

  // UI snapshots synced from BoardCoordinator
  const [mode, setMode] = useState<InstrumentMode>('free');
  const [exerciseId, setExerciseId] = useState<string>(PRESET_EXERCISES[0].id);
  const [customSteps, setCustomSteps] = useState<ExerciseStep[]>([]);
  const [left, setLeft] = useState(0.36);
  const [right, setRight] = useState(0.36);
  const [leftX, setLeftX] = useState(0.28);
  const [rightX, setRightX] = useState(0.72);
  const [personPresent, setPersonPresent] = useState(true);
  const [knobValue, setKnobValue] = useState(50);
  const [tolerance, setTolerance] = useState(0.17);
  const [bpm, setBpm] = useState(92);
  const [holdProgress, setHoldProgress] = useState(0);
  const [pixels, setPixels] = useState<PixelsPattern>('idle');
  const [currentStep, setCurrentStep] = useState<StepMessage | null>(null);
  const [sessionSummary, setSessionSummary] = useState<SessionSummary | null>(null);
  const [ripples, setRipples] = useState<WristRipple[]>([]);
  const [wireLogs, setWireLogs] = useState<ContractWireLog[]>([]);
  const [totalNotes, setTotalNotes] = useState(0);

  // Real Camera Feed & Tracking state (live camera enabled by default!)
  const [inputSource, setInputSource] = useState<PoseInputSource>('webcam');
  const [cameraStatus, setCameraStatus] = useState<CameraFeedStatus>('initializing');
  const [videoElement, setVideoElement] = useState<HTMLVideoElement | null>(null);
  const [cameraDevices, setCameraDevices] = useState<CameraDeviceOption[]>([]);
  const [selectedCameraId, setSelectedCameraId] = useState<string>('');
  const [mirrorVideo, setMirrorVideo] = useState(true);
  const [opticalFlickEnabled, setOpticalFlickEnabled] = useState(true);
  const [webcamError, setWebcamError] = useState<string | null>(null);
  const [autoPulse, setAutoPulse] = useState(false);

  const manualPoseRef = useRef({ left: 0.36, right: 0.36, personPresent: true });
  const opticalFlickRef = useRef(true);
  opticalFlickRef.current = opticalFlickEnabled;

  useEffect(() => {
    const board = boardRef.current!;
    board.setCallbacks({
      onNoteOn: (msg) => {
        const lat = synthRef.current!.noteOn(msg.note, msg.velocity, msg.voice, msg.ts_ms);
        setLastLatencyMs(lat);
        setTotalNotes(board.totalNotesPlayed);
        setRipples((prev) => [
          ...prev.slice(-10),
          {
            id: rippleSeqRef.current++,
            side: msg.voice === 1 ? 'right' : 'left',
            bornPerfMs: performance.now(),
            noteName: midiToNoteName(msg.note),
          },
        ]);
      },
      onTargetMelody: (msg) => {
        synthRef.current!.playMelody(msg.kind, msg.notes);
      },
      onState: (st) => {
        setLeft(st.left);
        setRight(st.right);
        setPersonPresent(st.person_present);
        setTolerance(st.tolerance);
        setBpm(st.bpm);
        setHoldProgress(st.hold_progress);
        setPixels(st.pixels);
      },
      onStep: (stepMsg) => {
        setCurrentStep(stepMsg);
      },
      onSessionDone: (summary) => {
        setSessionSummary(summary);
      },
      onWireLog: (entry) => {
        setWireLogs((prev) => [entry, ...prev].slice(0, 28));
      },
    });
  }, []);

  const startRealCamera = useCallback(async (preferredDeviceId?: string) => {
    const cam = webcamRef.current!;
    setWebcamError(null);
    setCameraStatus('initializing');
    const ok = await cam.start(preferredDeviceId);
    if (ok) {
      setVideoElement(cam.getVideoElement());
      setCameraStatus('live');
      setSelectedCameraId(cam.selectedDeviceId);
      setInputSource('webcam');
      const devs = await cam.listDevices();
      setCameraDevices(devs);
    } else {
      setVideoElement(null);
      setCameraStatus('blocked');
      setWebcamError('Allow browser camera access or start the test video stream.');
    }
    return ok;
  }, []);

  const startSyntheticCamera = useCallback(async () => {
    const cam = webcamRef.current!;
    setWebcamError(null);
    const ok = await cam.startSyntheticStream(() => ({
      left: manualPoseRef.current.left,
      right: manualPoseRef.current.right,
    }));
    if (ok) {
      setVideoElement(cam.getVideoElement());
      setCameraStatus('synthetic');
    }
  }, []);

  // Automatically start real camera feed on mount (or synthetic test stream if ?simcam=1)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('autostart') === '1') {
      setAudioStarted(true);
    }
    if (params.get('simcam') === '1') {
      startSyntheticCamera();
    } else {
      startRealCamera();
    }
    return () => {
      webcamRef.current?.stop();
    };
  }, [startRealCamera, startSyntheticCamera]);

  const ensureAudioStarted = useCallback(async () => {
    const synth = synthRef.current!;
    if (!synth.ready) {
      await synth.start();
    }
    if (!audioStarted) {
      setAudioStarted(true);
      setAudioMuted(false);
      synth.setMuted(false);
    }
  }, [audioStarted]);

  const handlePrimaryAudioButton = async () => {
    const synth = synthRef.current!;
    if (!audioStarted || !synth.ready) {
      await synth.start();
      setAudioStarted(true);
      setAudioMuted(false);
      synth.setMuted(false);
      boardRef.current!.mcuStrike(0.62);
    } else {
      const nextMute = !audioMuted;
      setAudioMuted(nextMute);
      synth.setMuted(nextMute);
    }
  };

  // 15 Hz Board Pose & Instruct Hold Loop (`python/main.py` equivalent)
  useEffect(() => {
    let lastMono = performance.now();
    const timer = window.setInterval(() => {
      const nowPerf = performance.now();
      const dtSec = clamp((nowPerf - lastMono) / 1000, 0.01, 0.25);
      lastMono = nowPerf;
      const board = boardRef.current!;
      const cam = webcamRef.current!;

      if (inputSource === 'webcam' && cam.active && cam.status === 'live') {
        const reading = cam.read();
        manualPoseRef.current.left = reading.left;
        manualPoseRef.current.right = reading.right;
        setLeftX(reading.leftX);
        setRightX(reading.rightX);

        if (opticalFlickRef.current && reading.opticalFlickIntensity > 0) {
          board.mcuStrike(reading.opticalFlickIntensity);
        }

        board.tick(dtSec, {
          left: reading.left,
          right: reading.right,
          personPresent: manualPoseRef.current.personPresent && reading.personPresent,
          fps: reading.fps,
        });
      } else if (inputSource === 'auto_patient') {
        let targetL = 0.5 + 0.34 * Math.sin(nowPerf * 0.0014);
        let targetR = 0.5 + 0.34 * Math.sin(nowPerf * 0.0011 + 1.1);
        const stepMsg = board.getCurrentStepMessage();
        if (board.mode === 'instruct' && stepMsg) {
          targetL = stepMsg.left;
          targetR = stepMsg.right;
        }
        const curL = manualPoseRef.current.left + (targetL - manualPoseRef.current.left) * 0.16;
        const curR = manualPoseRef.current.right + (targetR - manualPoseRef.current.right) * 0.16;
        manualPoseRef.current.left = curL;
        manualPoseRef.current.right = curR;
        board.tick(dtSec, {
          left: curL,
          right: curR,
          personPresent: manualPoseRef.current.personPresent,
          fps: 24,
        });
      } else {
        // Manual / Drag override (still renders live or test video behind the AR markers!)
        if (cam.active && cam.status === 'synthetic') {
          const reading = cam.read();
          setLeftX(reading.leftX);
          setRightX(reading.rightX);
        }
        board.tick(dtSec, {
          left: manualPoseRef.current.left,
          right: manualPoseRef.current.right,
          personPresent: manualPoseRef.current.personPresent,
          fps: 30,
        });
      }
    }, 66);

    return () => window.clearInterval(timer);
  }, [inputSource]);

  // Optional auto wrist strike pulse (92 BPM)
  useEffect(() => {
    if (!autoPulse) return;
    const msPerBeat = Math.round(60000 / Math.max(40, bpm));
    const id = window.setInterval(() => {
      boardRef.current?.mcuStrike(0.64);
    }, msPerBeat);
    return () => window.clearInterval(id);
  }, [autoPulse, bpm]);

  // Handlers
  const handleWristStrike = useCallback(
    async (intensity = 0.76) => {
      await ensureAudioStarted();
      boardRef.current!.mcuStrike(intensity);
    },
    [ensureAudioStarted]
  );

  const handleSetMode = useCallback((nextMode: InstrumentMode) => {
    const board = boardRef.current!;
    board.browserSetMode(nextMode);
    setMode(board.mode);
    setSessionSummary(board.lastSessionSummary);
    setCurrentStep(board.getCurrentStepMessage());
  }, []);

  const handleStartSession = useCallback(
    async (id: string) => {
      await ensureAudioStarted();
      const board = boardRef.current!;
      board.browserStartSession(id);
      setExerciseId(board.exerciseId);
      setMode(board.mode);
      setSessionSummary(null);
      setCurrentStep(board.getCurrentStepMessage());
      setActiveTab('stage');
    },
    [ensureAudioStarted]
  );

  const handleSelectExercise = useCallback((id: string) => {
    setExerciseId(id);
    const board = boardRef.current!;
    if (board.mode === 'instruct') {
      board.browserStartSession(id);
      setExerciseId(board.exerciseId);
      setSessionSummary(null);
      setCurrentStep(board.getCurrentStepMessage());
    } else {
      board.exerciseId = id;
    }
  }, []);

  const handleRecordCustomStep = useCallback(() => {
    const board = boardRef.current!;
    board.browserRecordStep(1.4);
    setCustomSteps([...board.customSteps]);
  }, []);

  const handleClearCustomSteps = useCallback(() => {
    const board = boardRef.current!;
    board.browserClearCustom();
    setCustomSteps([]);
    setExerciseId(board.exerciseId);
  }, []);

  const handleChangeInputSource = useCallback(
    async (src: PoseInputSource) => {
      if (src === 'webcam') {
        if (webcamRef.current?.status !== 'live') {
          await startRealCamera(selectedCameraId || undefined);
        } else {
          setInputSource('webcam');
        }
      } else {
        // Keep the live camera video visible on the stage even while holding manual/auto arm targets!
        setInputSource(src);
      }
    },
    [selectedCameraId, startRealCamera]
  );

  const handleSelectCameraDevice = useCallback(
    async (deviceId: string) => {
      setSelectedCameraId(deviceId);
      await startRealCamera(deviceId);
    },
    [startRealCamera]
  );

  const handleToggleMirror = useCallback((mirror: boolean) => {
    setMirrorVideo(mirror);
    if (webcamRef.current) {
      webcamRef.current.mirror = mirror;
    }
  }, []);

  const handleSetArmLevels = useCallback((nextL: number, nextR: number) => {
    setInputSource('manual');
    manualPoseRef.current.left = clamp(nextL, 0, 1);
    manualPoseRef.current.right = clamp(nextR, 0, 1);
    setLeft(manualPoseRef.current.left);
    setRight(manualPoseRef.current.right);
  }, []);

  const handleDragArm = useCallback((side: 'left' | 'right', nextLevel: number) => {
    setInputSource('manual');
    if (side === 'left') {
      manualPoseRef.current.left = clamp(nextLevel, 0, 1);
      setLeft(manualPoseRef.current.left);
    } else {
      manualPoseRef.current.right = clamp(nextLevel, 0, 1);
      setRight(manualPoseRef.current.right);
    }
  }, []);

  const handleMatchTargetPose = useCallback(async () => {
    await ensureAudioStarted();
    const step = boardRef.current!.getCurrentStepMessage();
    if (!step) return;
    setInputSource('manual');
    manualPoseRef.current.left = step.left;
    manualPoseRef.current.right = step.right;
    manualPoseRef.current.personPresent = true;
    setLeft(step.left);
    setRight(step.right);
    setPersonPresent(true);
    boardRef.current!.mcuStrike(0.68);
  }, [ensureAudioStarted]);

  const handleKnobChange = useCallback((val: number) => {
    setKnobValue(val);
    boardRef.current!.mcuKnob(val);
    setTolerance(boardRef.current!.tolerance);
  }, []);

  const handleModulinoButton = useCallback((btnId: 0 | 1 | 2) => {
    const board = boardRef.current!;
    board.mcuButton(btnId);
    setMode(board.mode);
    setExerciseId(board.exerciseId);
    setCustomSteps([...board.customSteps]);
    setSessionSummary(board.lastSessionSummary);
    setCurrentStep(board.getCurrentStepMessage());
  }, []);

  // Global keyboard shortcuts
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

      if (e.code === 'Space') {
        e.preventDefault();
        handleWristStrike(0.76);
      } else if (e.key === 'w' || e.key === 'W') {
        handleSetArmLevels(manualPoseRef.current.left + 0.08, manualPoseRef.current.right);
      } else if (e.key === 's' || e.key === 'S') {
        handleSetArmLevels(manualPoseRef.current.left - 0.08, manualPoseRef.current.right);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        handleSetArmLevels(manualPoseRef.current.left, manualPoseRef.current.right + 0.08);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        handleSetArmLevels(manualPoseRef.current.left, manualPoseRef.current.right - 0.08);
      } else if (e.key === 'm' || e.key === 'M') {
        handleSetMode(boardRef.current!.mode === 'free' ? 'instruct' : 'free');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [handleSetArmLevels, handleSetMode, handleWristStrike]);

  const primaryInstruction =
    mode === 'instruct'
      ? sessionSummary
        ? 'Wonderful movement — take a calm, comfortable breath'
        : currentStep?.instruction || 'Select Start Guided Session to begin'
      : !personPresent
        ? 'Camera paused — tap your wrist to play gentle arpeggio chords'
        : 'Move your arms in the camera view and flick your wrist to play';

  return (
    <div
      className="min-h-screen flex flex-col bg-[#FAF8F5] text-stone-900"
      data-mode={mode}
      data-audio={audioStarted && !audioMuted ? 'active' : 'locked'}
    >
      {/* Strict 3-Zone Top Bar Contract */}
      <header className="flex items-center justify-between gap-8 px-6 lg:px-10 py-4 bg-white border-b border-stone-200">
        <a
          href="#stage"
          onClick={(e) => {
            e.preventDefault();
            setActiveTab('stage');
          }}
          className="font-display text-xl font-semibold tracking-tight text-stone-900 whitespace-nowrap shrink-0"
        >
          Kinesthesia
        </a>

        <nav
          aria-label="Primary Navigation"
          className="hidden md:flex items-center gap-7 text-sm font-medium text-stone-600"
        >
          <button
            type="button"
            onClick={() => setActiveTab('stage')}
            className={`hover:text-stone-900 transition-colors whitespace-nowrap shrink-0 cursor-pointer pb-0.5 ${
              activeTab === 'stage' ? 'text-stone-900 border-b-2 border-teal-700 font-semibold' : ''
            }`}
          >
            Instrument Stage
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('exercises')}
            className={`hover:text-stone-900 transition-colors whitespace-nowrap shrink-0 cursor-pointer pb-0.5 ${
              activeTab === 'exercises'
                ? 'text-stone-900 border-b-2 border-teal-700 font-semibold'
                : ''
            }`}
          >
            Seated Exercises
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('protocol')}
            className={`hover:text-stone-900 transition-colors whitespace-nowrap shrink-0 cursor-pointer pb-0.5 ${
              activeTab === 'protocol'
                ? 'text-stone-900 border-b-2 border-teal-700 font-semibold'
                : ''
            }`}
          >
            Protocol &amp; Files
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('checklist')}
            className={`hover:text-stone-900 transition-colors whitespace-nowrap shrink-0 cursor-pointer pb-0.5 ${
              activeTab === 'checklist'
                ? 'text-stone-900 border-b-2 border-teal-700 font-semibold'
                : ''
            }`}
          >
            Board Checklist
          </button>
        </nav>

        <div className="shrink-0">
          <button
            type="button"
            onClick={handlePrimaryAudioButton}
            className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
              !audioStarted
                ? 'bg-teal-700 hover:bg-teal-800 text-white'
                : audioMuted
                  ? 'bg-stone-200 hover:bg-stone-300 text-stone-800'
                  : 'bg-stone-900 hover:bg-stone-800 text-white'
            }`}
          >
            {!audioStarted ? 'Start Audio' : audioMuted ? 'Unmute Audio' : 'Mute Audio'}
          </button>
        </div>
      </header>

      {/* Mobile Secondary Tab Bar */}
      <div className="flex md:hidden items-center gap-4 px-6 py-2.5 bg-white border-b border-stone-200 overflow-x-auto text-xs font-semibold text-stone-600">
        <button type="button" onClick={() => setActiveTab('stage')} className={activeTab === 'stage' ? 'text-teal-800' : ''}>
          Stage
        </button>
        <button type="button" onClick={() => setActiveTab('exercises')} className={activeTab === 'exercises' ? 'text-teal-800' : ''}>
          Exercises
        </button>
        <button type="button" onClick={() => setActiveTab('protocol')} className={activeTab === 'protocol' ? 'text-teal-800' : ''}>
          Protocol
        </button>
        <button type="button" onClick={() => setActiveTab('checklist')} className={activeTab === 'checklist' ? 'text-teal-800' : ''}>
          Checklist
        </button>
      </div>

      {/* Main Content Container */}
      <main className="flex-1 w-full max-w-[1380px] mx-auto px-6 lg:px-10 py-5">
        {activeTab === 'stage' ? (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
            {/* Left Zone (8 of 12 cols): Live Camera Feed Stage + Modulino Hardware Strip */}
            <div className="lg:col-span-8">
              <section
                aria-label="Player Screen"
                className="rounded-xl border border-stone-200 bg-white p-5"
              >
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-stone-500 mb-1.5">
                  <div>
                    <span>Live Camera Instrument</span>
                    <span aria-hidden="true" className="mx-2">·</span>
                    <span>C4 Major Pentatonic</span>
                    <span aria-hidden="true" className="mx-2">·</span>
                    <span>Local Browser Stream (Zero Saved Frames)</span>
                  </div>
                  <div className="font-mono tabular-nums text-stone-600">
                    {mode === 'instruct' && currentStep
                      ? `${currentStep.exercise_name} · Step ${currentStep.index + 1} of ${currentStep.total} · Hold ${Math.round(holdProgress * 100)}%`
                      : `Notes Played: ${totalNotes} · ${midiToNoteName(armHeightToMidi(left))} + ${midiToNoteName(armHeightToMidi(right))}`}
                  </div>
                </div>

                <h1
                  className="font-display text-2xl sm:text-3xl lg:text-[2rem] font-medium text-stone-900 leading-tight tracking-tight mb-3.5"
                  style={{ textWrap: 'balance' }}
                >
                  {primaryInstruction}
                </h1>

                {!audioStarted && (
                  <div className="mb-3.5 flex flex-wrap items-center justify-between gap-3 rounded-lg bg-[#F5F2EB] border border-stone-200 px-4 py-2.5">
                    <p className="text-xs sm:text-sm text-stone-700">
                      Click <strong>Start Audio</strong> once to unlock Web Audio synthesis (pre-allocates marimba and bell voices for sub-100 ms response).
                    </p>
                    <button
                      type="button"
                      onClick={handlePrimaryAudioButton}
                      className="px-3.5 py-1.5 rounded-lg bg-teal-700 hover:bg-teal-800 text-white text-xs font-semibold whitespace-nowrap cursor-pointer"
                    >
                      Start Audio Now
                    </button>
                  </div>
                )}

                {/* Real Mirrored Camera Feed + AR Musical Pitch & Target Stage */}
                <StageCanvas
                  mode={mode}
                  left={left}
                  right={right}
                  leftX={leftX}
                  rightX={rightX}
                  personPresent={personPresent}
                  holdProgress={holdProgress}
                  currentStep={currentStep}
                  ripples={ripples}
                  videoElement={videoElement}
                  cameraStatus={cameraStatus}
                  mirrorVideo={mirrorVideo}
                  interactiveDrag={true}
                  onDragArm={handleDragArm}
                  onWristTap={() => handleWristStrike(0.72)}
                  onRequestRealCamera={() => startRealCamera(selectedCameraId || undefined)}
                  onStartSyntheticCamera={startSyntheticCamera}
                />

                {sessionSummary && (
                  <div
                    role="region"
                    aria-label="Session Complete Summary"
                    className="mt-5 rounded-xl border border-teal-700/40 bg-teal-50/70 p-5 flex flex-wrap items-center justify-between gap-4"
                  >
                    <div>
                      <h2 className="font-display text-xl font-semibold text-stone-900">
                        {sessionSummary.exercise_name} Complete
                      </h2>
                      <p className="text-sm text-stone-700 mt-1 font-mono tabular-nums">
                        {sessionSummary.steps_completed} of {sessionSummary.total_steps} steps completed ·{' '}
                        {sessionSummary.duration_sec.toFixed(1)} seconds ·{' '}
                        {sessionSummary.notes_played} musical notes played
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => handleStartSession(exerciseId)}
                      className="px-5 py-2.5 rounded-lg bg-teal-700 hover:bg-teal-800 text-white text-sm font-semibold transition-colors whitespace-nowrap cursor-pointer"
                    >
                      Repeat Session
                    </button>
                  </div>
                )}
              </section>

              <ModulinoStrip
                pixels={pixels}
                knobValue={knobValue}
                tolerance={tolerance}
                bpm={bpm}
                lastLatencyMs={lastLatencyMs}
                onKnobChange={handleKnobChange}
                onButtonPress={handleModulinoButton}
                onWristStrike={handleWristStrike}
              />
            </div>

            {/* Right Zone (4 of 12 cols): Physiotherapist & Camera Controls */}
            <div className="lg:col-span-4">
              <PhysioPanel
                mode={mode}
                exerciseId={exerciseId}
                customSteps={customSteps}
                inputSource={inputSource}
                cameraStatus={cameraStatus}
                cameraDevices={cameraDevices}
                selectedCameraId={selectedCameraId}
                mirrorVideo={mirrorVideo}
                opticalFlickEnabled={opticalFlickEnabled}
                webcamError={webcamError}
                left={left}
                right={right}
                personPresent={personPresent}
                autoPulse={autoPulse}
                currentStep={currentStep}
                onSetMode={handleSetMode}
                onSelectExercise={handleSelectExercise}
                onStartSession={handleStartSession}
                onRecordCustomStep={handleRecordCustomStep}
                onClearCustomSteps={handleClearCustomSteps}
                onChangeInputSource={handleChangeInputSource}
                onSelectCameraDevice={handleSelectCameraDevice}
                onToggleMirrorVideo={handleToggleMirror}
                onToggleOpticalFlick={setOpticalFlickEnabled}
                onStartSyntheticCamera={startSyntheticCamera}
                onSetArmLevels={handleSetArmLevels}
                onTogglePersonPresent={(present) => {
                  manualPoseRef.current.personPresent = present;
                  setPersonPresent(present);
                }}
                onToggleAutoPulse={async (enabled) => {
                  if (enabled) await ensureAudioStarted();
                  setAutoPulse(enabled);
                }}
                onMatchTargetPose={handleMatchTargetPose}
              />
            </div>
          </div>
        ) : (
          <SwarmInspectorView
            activeSubTab={activeTab}
            wireLogs={wireLogs}
            onStartPresetFromCard={(id) => handleStartSession(id)}
          />
        )}
      </main>

      <footer className="mt-auto border-t border-stone-200 bg-white px-6 lg:px-10 py-4 text-xs text-stone-500 flex flex-wrap items-center justify-between gap-4">
        <div>
          Kinesthesia for Arduino UNO Q · Live Camera Movement-to-Music Instrument
        </div>
        <div className="flex items-center gap-4">
          <a href="/assets/index.html" target="_blank" rel="noreferrer" className="hover:text-stone-900 underline">
            App Lab Web UI (assets/index.html)
          </a>
          <span aria-hidden="true">·</span>
          <a href="/assets/synth-test.html" target="_blank" rel="noreferrer" className="hover:text-stone-900 underline">
            Offline Synth Test (assets/synth-test.html)
          </a>
        </div>
      </footer>
    </div>
  );
}
