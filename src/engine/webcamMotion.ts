/**
 * Live Camera Feed & Optical Arm/Wrist Tracker (`src/engine/webcamMotion.ts`)
 * - Streams real `getUserMedia` video directly to the stage canvas (zero frames
 *   saved to disk or sent over the network).
 * - Computes smoothed 2D mirrored left/right wrist centroids and vertical pitch
 *   levels in [0.0, 1.0], plus optical wrist-flick velocity for optional
 *   camera-driven note strikes when no physical IMU is attached.
 */

import { clamp } from './contract';

export interface CameraDeviceOption {
  deviceId: string;
  label: string;
}

export interface WebcamArmReading {
  left: number;
  right: number;
  leftX: number;   // 0..1 from left edge of mirrored stage
  rightX: number;  // 0..1 from left edge of mirrored stage
  opticalFlickIntensity: number; // > 0 when a crisp wrist flick is detected
  personPresent: boolean;
  fps: number;
}

export type CameraFeedStatus = 'initializing' | 'live' | 'synthetic' | 'blocked';

export class PrivacySafeWebcamEstimator {
  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;
  private procCanvas: HTMLCanvasElement | null = null;
  private procCtx: CanvasRenderingContext2D | null = null;
  private prevLuma: Float32Array | null = null;

  private synthCanvas: HTMLCanvasElement | null = null;
  private synthRafId = 0;

  private smoothLeft = 0.36;
  private smoothRight = 0.36;
  private smoothLeftX = 0.28;
  private smoothRightX = 0.72;
  private prevVelLeft = 0;
  private prevVelRight = 0;
  private lastFlickMs = 0;

  private lastTick = performance.now();
  private fpsEma = 30;

  active = false;
  status: CameraFeedStatus = 'initializing';
  mirror = true;
  selectedDeviceId = '';

  async listDevices(): Promise<CameraDeviceOption[]> {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.enumerateDevices) {
      return [];
    }
    try {
      const list = await navigator.mediaDevices.enumerateDevices();
      return list
        .filter((d) => d.kind === 'videoinput')
        .map((d, idx) => ({
          deviceId: d.deviceId,
          label: d.label || `Camera ${idx + 1}`,
        }));
    } catch {
      return [];
    }
  }

  async start(preferredDeviceId?: string): Promise<boolean> {
    this.stop();
    this.status = 'initializing';

    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      this.status = 'blocked';
      return false;
    }

    try {
      const videoConstraints: MediaTrackConstraints = preferredDeviceId
        ? {
            deviceId: { exact: preferredDeviceId },
            width: { ideal: 960 },
            height: { ideal: 540 },
          }
        : {
            facingMode: 'user',
            width: { ideal: 960 },
            height: { ideal: 540 },
          };

      this.stream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints,
        audio: false,
      });

      const track = this.stream.getVideoTracks()[0];
      this.selectedDeviceId = track?.getSettings?.().deviceId || preferredDeviceId || '';

      await this.attachStreamToVideo(this.stream, 'live');
      return true;
    } catch {
      this.stop();
      this.status = 'blocked';
      return false;
    }
  }

  /**
   * Starts a genuine `MediaStream` via `canvas.captureStream(30)` when testing
   * on a desktop/CI machine without a physical `/dev/video0` webcam attached.
   */
  async startSyntheticStream(getTargetLevels?: () => { left: number; right: number }): Promise<boolean> {
    this.stop();
    if (typeof document === 'undefined') return false;

    const cvs = document.createElement('canvas');
    cvs.width = 640;
    cvs.height = 360;
    const ctx = cvs.getContext('2d');
    if (!ctx || typeof cvs.captureStream !== 'function') {
      this.status = 'blocked';
      return false;
    }
    this.synthCanvas = cvs;

    const renderLoop = (now: number) => {
      const t = now * 0.001;
      const lvls = getTargetLevels
        ? getTargetLevels()
        : {
            left: 0.5 + 0.32 * Math.sin(t * 1.3),
            right: 0.5 + 0.32 * Math.sin(t * 1.05 + 0.9),
          };

      // Optical camera test pattern backdrop (used only when no physical webcam is attached)
      const grad = ctx.createLinearGradient(0, 0, 0, cvs.height);
      grad.addColorStop(0, '#1C1917');
      grad.addColorStop(1, '#0C0A09');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, cvs.width, cvs.height);

      // Subtle optical sensor calibration grid
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
      ctx.lineWidth = 1;
      for (let gx = 64; gx < cvs.width; gx += 64) {
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, cvs.height);
        ctx.stroke();
      }
      for (let gy = 60; gy < cvs.height; gy += 60) {
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(cvs.width, gy);
        ctx.stroke();
      }

      // Moving optical beacons in sensor space (mirror=true flips X onto Left/Right stage halves)
      const cx = cvs.width * 0.5;
      const lWristY = cvs.height - 38 - clamp(lvls.left, 0, 1) * (cvs.height - 76);
      const rWristY = cvs.height - 38 - clamp(lvls.right, 0, 1) * (cvs.height - 76);

      [
        { x: cx + 145, y: lWristY, glow: 'rgba(251, 191, 36, 0.28)' },
        { x: cx - 145, y: rWristY, glow: 'rgba(56, 189, 248, 0.28)' },
      ].forEach((b) => {
        ctx.fillStyle = b.glow;
        ctx.beginPath();
        ctx.arc(b.x, b.y, 34, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = '#F5F5F4';
        ctx.beginPath();
        ctx.arc(b.x, b.y, 14, 0, Math.PI * 2);
        ctx.fill();
      });

      this.synthRafId = window.requestAnimationFrame(renderLoop);
    };
    this.synthRafId = window.requestAnimationFrame(renderLoop);

    const stream = cvs.captureStream(30);
    this.stream = stream;
    await this.attachStreamToVideo(stream, 'synthetic');
    return true;
  }

  private async attachStreamToVideo(
    stream: MediaStream,
    nextStatus: 'live' | 'synthetic'
  ): Promise<void> {
    const vid = document.createElement('video');
    vid.playsInline = true;
    vid.muted = true;
    vid.autoplay = true;
    vid.srcObject = stream;
    await vid.play();

    const cvs = document.createElement('canvas');
    cvs.width = 160;
    cvs.height = 120;
    this.video = vid;
    this.procCanvas = cvs;
    this.procCtx = cvs.getContext('2d', { willReadFrequently: true });
    this.prevLuma = null;
    this.active = true;
    this.status = nextStatus;
  }

  getVideoElement(): HTMLVideoElement | null {
    return this.video;
  }

  read(): WebcamArmReading {
    if (
      !this.active ||
      !this.video ||
      !this.procCtx ||
      !this.procCanvas ||
      this.video.readyState < 2
    ) {
      return {
        left: this.smoothLeft,
        right: this.smoothRight,
        leftX: this.smoothLeftX,
        rightX: this.smoothRightX,
        opticalFlickIntensity: 0,
        personPresent: false,
        fps: 0,
      };
    }

    const now = performance.now();
    const dtMs = Math.max(4, now - this.lastTick);
    this.lastTick = now;
    this.fpsEma = 0.85 * this.fpsEma + 0.15 * (1000 / dtMs);

    const w = this.procCanvas.width;
    const h = this.procCanvas.height;
    this.procCtx.drawImage(this.video, 0, 0, w, h);
    const img = this.procCtx.getImageData(0, 0, w, h);
    const data = img.data;
    this.procCtx.clearRect(0, 0, w, h);

    if (!this.prevLuma || this.prevLuma.length !== w * h) {
      this.prevLuma = new Float32Array(w * h);
      for (let i = 0; i < w * h; i++) {
        const p = i * 4;
        this.prevLuma[i] = 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
      }
      return {
        left: this.smoothLeft,
        right: this.smoothRight,
        leftX: this.smoothLeftX,
        rightX: this.smoothRightX,
        opticalFlickIntensity: 0,
        personPresent: true,
        fps: Math.round(this.fpsEma),
      };
    }

    let sumWeightLeft = 0;
    let sumYLeft = 0;
    let sumXLeft = 0;

    let sumWeightRight = 0;
    let sumYRight = 0;
    let sumXRight = 0;

    for (let y = 6; y < h - 6; y++) {
      for (let x = 6; x < w - 6; x++) {
        const idx = y * w + x;
        const p = idx * 4;
        const luma = 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
        const diff = Math.abs(luma - this.prevLuma[idx]);
        this.prevLuma[idx] = 0.72 * this.prevLuma[idx] + 0.28 * luma;

        if (diff > 10) {
          const normHeight = 1 - (y - 6) / (h - 12);
          const screenX = this.mirror ? 1 - x / w : x / w;

          if (screenX < 0.48) {
            sumWeightLeft += diff;
            sumYLeft += normHeight * diff;
            sumXLeft += screenX * diff;
          } else if (screenX > 0.52) {
            sumWeightRight += diff;
            sumYRight += normHeight * diff;
            sumXRight += screenX * diff;
          }
        }
      }
    }

    const prevL = this.smoothLeft;
    const prevR = this.smoothRight;

    if (sumWeightLeft > 240) {
      const targetL = clamp((sumYLeft / sumWeightLeft - 0.08) / 0.84, 0, 1);
      const targetLX = clamp(sumXLeft / sumWeightLeft, 0.12, 0.44);
      this.smoothLeft = 0.68 * this.smoothLeft + 0.32 * targetL;
      this.smoothLeftX = 0.75 * this.smoothLeftX + 0.25 * targetLX;
    }

    if (sumWeightRight > 240) {
      const targetR = clamp((sumYRight / sumWeightRight - 0.08) / 0.84, 0, 1);
      const targetRX = clamp(sumXRight / sumWeightRight, 0.56, 0.88);
      this.smoothRight = 0.68 * this.smoothRight + 0.32 * targetR;
      this.smoothRightX = 0.75 * this.smoothRightX + 0.25 * targetRX;
    }

    // Detect crisp optical wrist flicks when user snaps wrist in front of webcam
    const velL = Math.abs(this.smoothLeft - prevL) / (dtMs / 1000);
    const velR = Math.abs(this.smoothRight - prevR) / (dtMs / 1000);
    const peakVel = Math.max(velL, velR);
    let flick = 0;
    if (
      peakVel > 0.95 &&
      peakVel < Math.max(this.prevVelLeft, this.prevVelRight) &&
      now - this.lastFlickMs > 220
    ) {
      flick = clamp((peakVel - 0.75) / 2.2, 0.25, 0.95);
      this.lastFlickMs = now;
    }
    this.prevVelLeft = velL;
    this.prevVelRight = velR;

    return {
      left: Math.round(this.smoothLeft * 1000) / 1000,
      right: Math.round(this.smoothRight * 1000) / 1000,
      leftX: Math.round(this.smoothLeftX * 1000) / 1000,
      rightX: Math.round(this.smoothRightX * 1000) / 1000,
      opticalFlickIntensity: flick,
      personPresent: true,
      fps: Math.round(this.fpsEma),
    };
  }

  stop(): void {
    this.active = false;
    if (this.synthRafId) {
      window.cancelAnimationFrame(this.synthRafId);
      this.synthRafId = 0;
    }
    this.synthCanvas = null;
    if (this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
    if (this.video) {
      this.video.srcObject = null;
      this.video = null;
    }
    this.prevLuma = null;
  }
}
