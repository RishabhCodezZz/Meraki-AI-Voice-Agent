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
    this.pending = 0; // chunks queued or decoding, not yet scheduled
    this.tail = Promise.resolve();
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

  /** True from the moment a chunk is queued, not just once it is scheduled. */
  get playing() {
    return this.sources.size > 0 || this.pending > 0;
  }

  /**
   * Queue one base64 MP3 chunk. Resolves once it has been scheduled.
   *
   * Calls run strictly one after another. Frames arrive in order but decodes
   * finish whenever they like, so left alone a short chunk can overtake a long
   * one and the words play out of order.
   */
  enqueue(base64) {
    const generation = this.generation;
    this.pending++;
    const task = this.tail
      .then(() => this.schedule(base64, generation))
      .finally(() => {
        this.pending--;
        // The last chunk may have been dropped with nothing playing to fire
        // onended, and someone is waiting to hear that we are done.
        if (generation === this.generation) this.settleIfIdle();
      });
    // One bad chunk must not poison the chain behind it.
    this.tail = task.catch(() => {});
    return task;
  }

  async schedule(base64, generation) {
    // Flushed while this waited its turn: skip the decode altogether.
    if (generation !== this.generation) return;
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
      this.settleIfIdle();
    };

    this.startLevels();
  }

  /** Report idle only when nothing is playing and nothing is on its way. */
  settleIfIdle() {
    if (this.sources.size > 0 || this.pending > 0) return;
    this.nextStart = 0;
    this.stopLevels();
    this.onIdle?.();
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
