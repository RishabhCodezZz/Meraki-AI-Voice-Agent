/**
 * Gapless playback for streamed speech chunks.
 *
 * Chunks arrive as base64 MP3 while the model is still writing. Each is decoded
 * and scheduled on the Web Audio clock immediately after the previous one, so
 * consecutive chunks join without the click or gap you get from queuing
 * <audio> elements.
 */

export class SpeechPlayer {
  constructor({ onLevel, onIdle }) {
    this.onLevel = onLevel;
    this.onIdle = onIdle;
    this.ctx = null;
    this.gain = null;
    this.analyser = null;
    this.sources = new Set();
    this.nextStart = 0;
    this.raf = 0;
    this.generation = 0;
  }

  async ensureContext() {
    if (!this.ctx) {
      this.ctx = new AudioContext({ latencyHint: 'playback' });
      this.gain = this.ctx.createGain();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.8;
      this.gain.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') await this.ctx.resume();
    return this.ctx;
  }

  get playing() {
    return this.sources.size > 0;
  }

  /** Queue one base64 MP3 chunk. Resolves once it has been scheduled. */
  async enqueue(base64) {
    const generation = this.generation;
    const ctx = await this.ensureContext();

    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

    let buffer;
    try {
      buffer = await ctx.decodeAudioData(bytes.buffer);
    } catch {
      // A malformed chunk should not take the whole reply down.
      console.warn('Dropped an undecodable audio chunk');
      return;
    }

    // A flush landed while we were decoding; this audio is stale.
    if (generation !== this.generation) return;

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain);

    const lead = 0.06; // small cushion so the first chunk never starts late
    const startAt = Math.max(ctx.currentTime + lead, this.nextStart);
    source.start(startAt);
    this.nextStart = startAt + buffer.duration;

    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      if (this.sources.size === 0) {
        this.nextStart = 0;
        this.stopLevels();
        this.onIdle?.();
      }
    };

    this.startLevels();
  }

  /** Barge-in: drop everything scheduled and not yet heard. */
  flush() {
    this.generation++;
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        /* already finished */
      }
    }
    this.sources.clear();
    this.nextStart = 0;
    this.stopLevels();
  }

  startLevels() {
    if (this.raf || !this.analyser) return;
    const bins = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (!this.analyser) return;
      this.analyser.getByteFrequencyData(bins);
      this.onLevel(bins);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  stopLevels() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  async close() {
    this.flush();
    if (this.ctx) {
      await this.ctx.close().catch(() => {});
      this.ctx = null;
      this.gain = null;
      this.analyser = null;
    }
  }
}
