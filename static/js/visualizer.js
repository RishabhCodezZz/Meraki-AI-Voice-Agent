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
// Safari only gained roundRect in 16.4. This runs every frame, so a missing
// method would throw sixty times a second and take the meter out completely.
const HAS_ROUND_RECT =
  typeof CanvasRenderingContext2D !== 'undefined' &&
  typeof CanvasRenderingContext2D.prototype.roundRect === 'function';

// The idle meter has to read as a meter, not as a flat line: 0.035 at 82px was
// under three pixels. The floor is the lowest the breathing wave can reach.
const IDLE_BASE = 0.07;
const IDLE_WAVE = 0.025;
// Reduced motion trades the breathing and the travelling pulse for fixed levels.
const THINKING_STATIC = 0.12;
// Mirrors the values in styles.css, for the moment before the stylesheet applies.
const FALLBACK_ACCENT = '#e8434a';
const FALLBACK_IDLE = '#33414a';

export class Visualizer {
  /** Lowest target any bar gets while idle; exposed so tests can pin it. */
  static idleFloor = IDLE_BASE - IDLE_WAVE;

  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.heights = new Float32Array(BARS);
    this.targets = new Float32Array(BARS);
    this.state = 'idle';
    this.t = 0;
    this.running = false;
    this.scheduled = false;
    // True when what is on screen no longer matches the state; only consulted
    // under reduced motion, where unchanged frames are not repainted.
    this.dirty = true;

    this.motionQuery = matchMedia('(prefers-reduced-motion: reduce)');
    this.reduced = this.motionQuery.matches;
    this.schemeQuery = matchMedia('(prefers-color-scheme: dark)');

    this.resize = this.resize.bind(this);
    this.refreshColors = this.refreshColors.bind(this);
    this.onMotionChange = () => {
      this.reduced = this.motionQuery.matches;
      this.dirty = true;
    };
    this.onVisibility = () => {
      if (!document.hidden) this.schedule();
    };
    window.addEventListener('resize', this.resize);
    this.schemeQuery.addEventListener?.('change', this.refreshColors);
    this.motionQuery.addEventListener?.('change', this.onMotionChange);
    document.addEventListener('visibilitychange', this.onVisibility);
    // resize() also resolves the palette, so this is the one read at start-up.
    this.resize();
  }

  /**
   * Palette is resolved here, not per frame: getComputedStyle forces style
   * recalculation and the meter draws sixty times a second.
   */
  refreshColors() {
    const styles = getComputedStyle(document.documentElement);
    this.accent = styles.getPropertyValue('--accent').trim() || FALLBACK_ACCENT;
    this.dim = styles.getPropertyValue('--bar-idle').trim() || FALLBACK_IDLE;
    this.dirty = true;
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = rect.width;
    this.h = rect.height;
    // Resizing a canvas clears it, and a new palette needs repainting too.
    this.refreshColors();
  }

  setState(state) {
    if (state !== this.state) this.dirty = true;
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
    this.schedule();
  }

  stop() {
    this.running = false;
  }

  /** One frame in flight at a time; a hidden tab lets the loop lapse. */
  schedule() {
    if (!this.running || this.scheduled || document.hidden) return;
    this.scheduled = true;
    requestAnimationFrame(() => {
      this.scheduled = false;
      if (!this.running) return;
      this.step();
      if (this.shouldDraw()) this.draw();
      this.schedule();
    });
  }

  /**
   * Under reduced motion the idle and thinking profiles are static, so once
   * the bars have eased into place there is nothing new to paint. Real audio
   * is never skipped: that is data, not decoration.
   */
  shouldDraw() {
    if (!this.reduced) return true;
    if (this.state === 'listening' || this.state === 'speaking') return true;
    return this.dirty;
  }

  step() {
    this.t += 0.016;

    if (this.state === 'thinking') {
      // A pulse travelling left to right.
      for (let i = 0; i < BARS; i++) {
        if (this.reduced) {
          this.targets[i] = THINKING_STATIC;
          continue;
        }
        const phase = (i / BARS) * Math.PI * 2 - this.t * 3.2;
        const pulse = Math.max(0, Math.sin(phase));
        this.targets[i] = 0.05 + Math.pow(pulse, 6) * 0.55;
      }
    } else if (this.state !== 'listening' && this.state !== 'speaking') {
      // idle, connecting and blocked: nothing is flowing, so all three rest.
      for (let i = 0; i < BARS; i++) {
        this.targets[i] = this.reduced
          ? IDLE_BASE
          : IDLE_BASE + Math.sin(this.t * 1.1 + i * 0.18) * IDLE_WAVE;
      }
    }

    let moved = false;
    for (let i = 0; i < BARS; i++) {
      const delta = (this.targets[i] - this.heights[i]) * EASE;
      this.heights[i] += delta;
      if (Math.abs(delta) > 0.0005) moved = true;
    }
    if (moved) this.dirty = true;
  }

  draw() {
    const { ctx, w, h, accent, dim } = this;
    ctx.clearRect(0, 0, w, h);
    this.dirty = false;

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

      if (HAS_ROUND_RECT) {
        const radius = Math.min(barWidth / 2, 2);
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, radius);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, barWidth, barHeight);
      }
    }

    ctx.globalAlpha = 1;
  }

  destroy() {
    this.stop();
    window.removeEventListener('resize', this.resize);
    this.schemeQuery.removeEventListener?.('change', this.refreshColors);
    this.motionQuery.removeEventListener?.('change', this.onMotionChange);
    document.removeEventListener('visibilitychange', this.onVisibility);
  }
}
