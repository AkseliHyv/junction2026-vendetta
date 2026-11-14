import React, { useEffect, useRef } from 'react';
import {
  armHeightToMidi,
  clamp,
  InstrumentMode,
  midiToNoteName,
  StepMessage,
} from '../engine/contract';
import { CameraFeedStatus } from '../engine/webcamMotion';

export interface WristRipple {
  id: number;
  side: 'left' | 'right';
  bornPerfMs: number;
  noteName: string;
}

interface StageCanvasProps {
  mode: InstrumentMode;
  left: number;
  right: number;
  leftX: number;
  rightX: number;
  personPresent: boolean;
  holdProgress: number;
  currentStep: StepMessage | null;
  ripples: WristRipple[];
  videoElement: HTMLVideoElement | null;
  cameraStatus: CameraFeedStatus;
  mirrorVideo: boolean;
  interactiveDrag: boolean;
  onDragArm?: (side: 'left' | 'right', nextLevel: number) => void;
  onWristTap?: () => void;
  onRequestRealCamera: () => void;
  onStartSyntheticCamera: () => void;
}

const TOP_STAFF_PAD = 44;
const BOT_STAFF_PAD = 46;

function levelToCanvasY(canvasH: number, level: number): number {
  const usableH = canvasH - TOP_STAFF_PAD - BOT_STAFF_PAD;
  return canvasH - BOT_STAFF_PAD - clamp(level, 0, 1) * usableH;
}

export const StageCanvas: React.FC<StageCanvasProps> = ({
  mode,
  left,
  right,
  leftX,
  rightX,
  personPresent,
  holdProgress,
  currentStep,
  ripples,
  videoElement,
  cameraStatus,
  mirrorVideo,
  interactiveDrag,
  onDragArm,
  onWristTap,
  onRequestRealCamera,
  onStartSyntheticCamera,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const draggingSideRef = useRef<'left' | 'right' | null>(null);

  const stateRef = useRef({
    mode,
    left,
    right,
    leftX,
    rightX,
    personPresent,
    holdProgress,
    currentStep,
    ripples,
    videoElement,
    cameraStatus,
    mirrorVideo,
    interactiveDrag,
  });
  stateRef.current = {
    mode,
    left,
    right,
    leftX,
    rightX,
    personPresent,
    holdProgress,
    currentStep,
    ripples,
    videoElement,
    cameraStatus,
    mirrorVideo,
    interactiveDrag,
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let rafId = 0;

    const render = (nowPerf: number) => {
      const s = stateRef.current;
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      // 1. Draw Live Real Camera Feed (or dark optical viewfinder background)
      const vid = s.videoElement;
      const hasLiveVideo =
        vid && vid.readyState >= 2 && vid.videoWidth > 0 && vid.videoHeight > 0;

      if (hasLiveVideo) {
        // Aspect-fill (`object-cover`) math so camera never stretches or distorts
        const vw = vid.videoWidth;
        const vh = vid.videoHeight;
        const scale = Math.max(w / vw, h / vh);
        const drawW = vw * scale;
        const drawH = vh * scale;
        const offsetX = (w - drawW) * 0.5;
        const offsetY = (h - drawH) * 0.5;

        ctx.save();
        if (s.mirrorVideo) {
          ctx.translate(w, 0);
          ctx.scale(-1, 1);
          ctx.drawImage(vid, -offsetX, offsetY, drawW, drawH);
        } else {
          ctx.drawImage(vid, offsetX, offsetY, drawW, drawH);
        }
        ctx.restore();

        // Measured contrast scrims along left pitch staff and bottom caption for >=4.5:1 legibility
        const leftScrim = ctx.createLinearGradient(0, 0, 120, 0);
        leftScrim.addColorStop(0, 'rgba(12, 10, 9, 0.68)');
        leftScrim.addColorStop(1, 'rgba(12, 10, 9, 0.0)');
        ctx.fillStyle = leftScrim;
        ctx.fillRect(0, 0, 120, h);

        const bottomScrim = ctx.createLinearGradient(0, h - 72, 0, h);
        bottomScrim.addColorStop(0, 'rgba(12, 10, 9, 0.0)');
        bottomScrim.addColorStop(1, 'rgba(12, 10, 9, 0.72)');
        ctx.fillStyle = bottomScrim;
        ctx.fillRect(0, h - 72, w, 72);
      } else {
        // Sleek dark camera viewfinder backdrop while awaiting camera permission
        const bgGrad = ctx.createLinearGradient(0, 0, 0, h);
        bgGrad.addColorStop(0, '#1C1917');
        bgGrad.addColorStop(1, '#0C0A09');
        ctx.fillStyle = bgGrad;
        ctx.fillRect(0, 0, w, h);
      }

      // 2. 8-Note C Major Pentatonic Horizontal Pitch Guides
      const scaleLabels = ['C4', 'D4', 'E4', 'G4', 'A4', 'C5', 'D5', 'E5'];
      const usableH = h - TOP_STAFF_PAD - BOT_STAFF_PAD;

      ctx.save();
      ctx.font = '600 13px "IBM Plex Mono", monospace';
      ctx.textBaseline = 'middle';
      for (let i = 0; i < 8; i++) {
        const bandCenter = (i + 0.5) / 8;
        const y = h - BOT_STAFF_PAD - bandCenter * usableH;

        ctx.strokeStyle = 'rgba(255, 255, 255, 0.18)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(56, y);
        ctx.lineTo(w - 24, y);
        ctx.stroke();

        ctx.fillStyle = 'rgba(250, 250, 249, 0.92)';
        ctx.fillText(scaleLabels[i], 18, y);
      }
      ctx.restore();

      // Compute wrist marker coordinates in stage space
      const leftPos = {
        x: clamp(s.leftX, 0.12, 0.45) * w,
        y: levelToCanvasY(h, s.left),
      };
      const rightPos = {
        x: clamp(s.rightX, 0.55, 0.88) * w,
        y: levelToCanvasY(h, s.right),
      };

      // 3. Guided Exercise Target Zones (AR Overlay in Instruct Mode — Zero Red)
      if (s.mode === 'instruct' && s.currentStep) {
        const haloRadius = clamp(s.currentStep.tolerance * usableH * 0.52, 28, 66);
        const targetLeft = { x: w * 0.28, y: levelToCanvasY(h, s.currentStep.left) };
        const targetRight = { x: w * 0.72, y: levelToCanvasY(h, s.currentStep.right) };

        [
          { pt: targetLeft, cur: leftPos, label: 'Target L' },
          { pt: targetRight, cur: rightPos, label: 'Target R' },
        ].forEach(({ pt, cur, label }) => {
          ctx.save();
          // Guide connector line from current hand to target zone
          ctx.setLineDash([6, 6]);
          ctx.strokeStyle = 'rgba(45, 212, 191, 0.55)';
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(cur.x, cur.y);
          ctx.lineTo(pt.x, pt.y);
          ctx.stroke();

          // Luminous translucent target zone
          ctx.setLineDash([]);
          ctx.fillStyle = 'rgba(20, 184, 166, 0.22)';
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, haloRadius, 0, Math.PI * 2);
          ctx.fill();

          ctx.setLineDash([8, 6]);
          ctx.strokeStyle = '#5EEAD4';
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, haloRadius, 0, Math.PI * 2);
          ctx.stroke();

          ctx.setLineDash([]);
          ctx.font = '700 12.5px "Plus Jakarta Sans", system-ui, sans-serif';
          ctx.fillStyle = '#CCFBF1';
          ctx.textAlign = 'center';
          const topLabelY = pt.y - haloRadius - 9;
          const labelY =
            Math.abs(cur.y - topLabelY) < 34 && Math.abs(cur.x - pt.x) < 70
              ? pt.y + haloRadius + 17
              : topLabelY;
          ctx.fillText(label, pt.x, labelY);
          ctx.restore();
        });

        // Top-Center Radial Hold Progress Ring on Video HUD
        const cx = w * 0.5;
        const ringY = 48;
        ctx.save();
        ctx.fillStyle = 'rgba(12, 10, 9, 0.55)';
        ctx.beginPath();
        ctx.arc(cx, ringY, 30, 0, Math.PI * 2);
        ctx.fill();

        ctx.strokeStyle = 'rgba(255, 255, 255, 0.22)';
        ctx.lineWidth = 6;
        ctx.beginPath();
        ctx.arc(cx, ringY, 24, 0, Math.PI * 2);
        ctx.stroke();

        if (s.holdProgress > 0.01) {
          ctx.strokeStyle = '#2DD4BF';
          ctx.lineWidth = 6;
          ctx.lineCap = 'round';
          ctx.beginPath();
          ctx.arc(
            cx,
            ringY,
            24,
            -Math.PI / 2,
            -Math.PI / 2 + Math.PI * 2 * s.holdProgress
          );
          ctx.stroke();
        }

        ctx.fillStyle = '#FFFFFF';
        ctx.font = '700 11px "IBM Plex Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(`${Math.round(s.holdProgress * 100)}%`, cx, ringY + 0.5);
        ctx.restore();
      }

      // 4. Live Left & Right Wrist Pitch Orbs Tracked Over Camera Feed
      const wristMarkers = [
        {
          pt: leftPos,
          fill: '#D97706',
          ring: '#FDE68A',
          note: midiToNoteName(armHeightToMidi(s.left)),
          sideLabel: 'L',
        },
        {
          pt: rightPos,
          fill: '#0284C7',
          ring: '#BAE6FD',
          note: midiToNoteName(armHeightToMidi(s.right)),
          sideLabel: 'R',
        },
      ];

      wristMarkers.forEach(({ pt, fill, ring, note, sideLabel }) => {
        ctx.save();
        // Horizontal laser tick showing exact alignment on pitch staff
        ctx.strokeStyle = ring;
        ctx.globalAlpha = 0.45;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(pt.x - 36, pt.y);
        ctx.lineTo(pt.x + 36, pt.y);
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Outer high-contrast halo for 2-metre legibility over any clothing/background
        ctx.fillStyle = 'rgba(12, 10, 9, 0.55)';
        ctx.beginPath();
        ctx.arc(pt.x, pt.y, 29, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = fill;
        ctx.strokeStyle = '#FFFFFF';
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(pt.x, pt.y, 24, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = '#FFFFFF';
        ctx.font = '700 13.5px "Plus Jakarta Sans", system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(note, pt.x, pt.y - 3);

        ctx.font = '600 9.5px "IBM Plex Mono", monospace';
        ctx.fillStyle = 'rgba(255, 255, 255, 0.88)';
        ctx.fillText(sideLabel, pt.x, pt.y + 11);
        ctx.restore();
      });

      // 5. Musical Wrist Strike Ripples
      s.ripples.forEach((rp) => {
        const age = (nowPerf - rp.bornPerfMs) / 700;
        if (age < 0 || age >= 1) return;
        const anchor = rp.side === 'right' ? rightPos : leftPos;
        const color = rp.side === 'right' ? '#38BDF8' : '#FBBF24';

        ctx.save();
        ctx.strokeStyle = color;
        ctx.globalAlpha = Math.pow(1 - age, 1.25);
        ctx.lineWidth = 6 * (1 - age) + 1.5;
        ctx.beginPath();
        ctx.arc(anchor.x, anchor.y, 26 + age * 88, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      });

      // 6. Bottom-Right Camera HUD Status Caption
      ctx.save();
      ctx.font = '500 12.5px "Plus Jakarta Sans", system-ui, sans-serif';
      ctx.fillStyle = 'rgba(250, 250, 249, 0.92)';
      ctx.textAlign = 'right';
      const statusText = !s.personPresent
        ? 'Wrist-only fallback active (arpeggio mode)'
        : s.cameraStatus === 'live'
          ? 'Live camera feed · Wave arms to set pitch & flick wrist or press Space'
          : s.cameraStatus === 'synthetic'
            ? 'Test camera stream active · Drag markers or wave to test'
            : 'Drag L/R pitch markers vertically or enable camera';
      ctx.fillText(statusText, w - 18, h - 15);
      ctx.restore();

      rafId = window.requestAnimationFrame(render);
    };

    rafId = window.requestAnimationFrame(render);
    return () => window.cancelAnimationFrame(rafId);
  }, []);

  const mapPointerToArm = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = (clientX - rect.left) * scaleX;
    const y = (clientY - rect.top) * scaleY;
    const side: 'left' | 'right' = x < canvas.width * 0.5 ? 'left' : 'right';
    const usableH = canvas.height - TOP_STAFF_PAD - BOT_STAFF_PAD;
    const level = clamp((canvas.height - BOT_STAFF_PAD - y) / usableH, 0, 1);
    return { side, level };
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactiveDrag) {
      onWristTap?.();
      return;
    }
    const mapped = mapPointerToArm(e.clientX, e.clientY);
    if (!mapped) return;
    draggingSideRef.current = mapped.side;
    e.currentTarget.setPointerCapture(e.pointerId);
    onDragArm?.(mapped.side, mapped.level);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactiveDrag || !draggingSideRef.current) return;
    const mapped = mapPointerToArm(e.clientX, e.clientY);
    if (!mapped) return;
    onDragArm?.(draggingSideRef.current, mapped.level);
  };

  const handlePointerUp = () => {
    if (draggingSideRef.current) {
      draggingSideRef.current = null;
      onWristTap?.();
    }
  };

  const showCameraPrompt = cameraStatus === 'blocked' || cameraStatus === 'initializing';

  return (
    <div className="relative w-full overflow-hidden rounded-xl border border-stone-800 bg-stone-950">
      <canvas
        ref={canvasRef}
        width={920}
        height={420}
        data-testid="pose-stage-canvas"
        data-camera-status={cameraStatus}
        aria-label="Live camera movement-to-music stage"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        className="block w-full h-auto touch-none cursor-grab active:cursor-grabbing"
      />

      {showCameraPrompt && (
        <div className="absolute top-3.5 right-3.5 flex flex-wrap items-center gap-2 rounded-lg bg-stone-900/90 border border-stone-700 px-3.5 py-2 text-xs text-stone-100 backdrop-blur-xs">
          <span>
            {cameraStatus === 'initializing'
              ? 'Starting camera…'
              : 'Camera permission needed or no webcam detected'}
          </span>
          <button
            type="button"
            onClick={onRequestRealCamera}
            className="px-2.5 py-1 rounded bg-teal-600 hover:bg-teal-500 text-white font-semibold whitespace-nowrap cursor-pointer"
          >
            Enable Camera
          </button>
          <button
            type="button"
            onClick={onStartSyntheticCamera}
            className="px-2.5 py-1 rounded border border-stone-600 bg-stone-800 hover:bg-stone-700 text-stone-200 font-medium whitespace-nowrap cursor-pointer"
          >
            Use Test Stream
          </button>
        </div>
      )}
    </div>
  );
};
