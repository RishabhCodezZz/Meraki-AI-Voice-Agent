/**
 * The centrepiece: a bar meter driven by real audio.
 *
 * Listening renders live microphone spectrum, speaking renders the spectrum of
 * the audio actually coming out of the player, and the idle and thinking states
 * are synthetic. Every bar height is eased toward its target so the meter reads
 * as fluid rather than strobing at frame rate.
 */

const BARS = 56;
const EASE = 0.28;

export class Visualizer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.heights = new Float32Array(BARS);
    this.targets = new Float32Array(BARS);
    this.state = 'idle';
    this.t = 0;
    this.running = false;

    this.resize = this.resize.bind(this);
    window.addEventListener('resize', this.resize);
    this.resize();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = rect.width;
    this.h = rect.height;
  }

  setState(state) {
    this.state = state;
  }

  /** Feed real spectrum data; only used while listening or speaking. */
  setSpectrum(bins) {
    if (this.state !== 'listening' && this.state !== 'speaking') return;
    // Voice energy sits low in the spectrum, so sample the bottom third and
    // spread it across the full bar count.
    const usable = Math.floor(bins.length * 0.34);
    for (let i = 0; i < BARS; i++) {
      const from = Math.floor((i / BARS) * usable);
      const to = Math.max(from + 1, Math.floor(((i + 1) / BARS) * usable));
      let sum = 0;
      for (let j = from; j < to; j++) sum += bins[j];
      const avg = sum / (to - from) / 255;
      // Perceptual curve - quiet speech should still move the meter.
      this.targets[i] = Math.min(1, Math.pow(avg, 0.72) * 1.35);
    }
  }

  start() {
    if (this.running) return;
    this.running = true;
    const frame = () => {
      if (!this.running) return;
      this.step();
      this.draw();
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  stop() {
    this.running = false;
  }

  step() {
    this.t += 0.016;

    if (this.state === 'idle' || this.state === 'error') {
      for (let i = 0; i < BARS; i++) {
        const wave = Math.sin(this.t * 1.1 + i * 0.18);
        this.targets[i] = 0.035 + wave * 0.02;
      }
    } else if (this.state === 'thinking') {
      // A pulse travelling left to right.
      for (let i = 0; i < BARS; i++) {
        const phase = (i / BARS) * Math.PI * 2 - this.t * 3.2;
        const pulse = Math.max(0, Math.sin(phase));
        this.targets[i] = 0.05 + Math.pow(pulse, 6) * 0.55;
      }
    }

    for (let i = 0; i < BARS; i++) {
      this.heights[i] += (this.targets[i] - this.heights[i]) * EASE;
    }
  }

  draw() {
    const { ctx, w, h } = this;
    ctx.clearRect(0, 0, w, h);

    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue('--accent').trim() || '#f5a524';
    const dim = styles.getPropertyValue('--bar-idle').trim() || '#2c2c33';

    const gap = 3;
    const barWidth = Math.max(2, (w - gap * (BARS - 1)) / BARS);
    const mid = h / 2;
    const live = this.state === 'listening' || this.state === 'speaking' || this.state === 'thinking';

    for (let i = 0; i < BARS; i++) {
      const amplitude = Math.max(0.012, this.heights[i]);
      const barHeight = amplitude * h * 0.92;
      const x = i * (barWidth + gap);
      const y = mid - barHeight / 2;

      if (live) {
        // Brighter toward the centre so the meter has a focal point.
        const centreBias = 1 - Math.abs(i / (BARS - 1) - 0.5) * 1.2;
        ctx.globalAlpha = 0.45 + centreBias * 0.55;
        ctx.fillStyle = accent;
      } else {
        ctx.globalAlpha = 1;
        ctx.fillStyle = dim;
      }

      const radius = Math.min(barWidth / 2, 2);
      ctx.beginPath();
      ctx.roundRect(x, y, barWidth, barHeight, radius);
      ctx.fill();
    }

    ctx.globalAlpha = 1;
  }

  destroy() {
    this.stop();
    window.removeEventListener('resize', this.resize);
  }
}
