/**
 * Microphone capture: getUserMedia -> AudioWorklet -> PCM16 frames.
 *
 * The mic is deliberately never connected to the destination. The old build
 * routed it to the speakers, which fed the assistant's own voice back into the
 * transcriber.
 */

export class MicCapture {
  constructor({ onFrame, onLevel, onEnded }) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    // Called when the microphone itself goes away (unplugged, permission
    // revoked), as opposed to stop() being called.
    this.onEnded = onEnded;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.analyser = null;
    this.raf = 0;
    this.stopped = false;
  }

  get active() {
    return Boolean(this.node);
  }

  /**
   * Create the AudioContext. Call this first thing inside the click handler:
   * Safari and iOS only let a context start if it is made during the gesture,
   * and by the time start() runs a network round trip has used the gesture up.
   * The context is made before any await so that holds.
   */
  async prepare() {
    if (!this.ctx) this.ctx = this.makeContext(true);
    // Safari and iOS start contexts suspended until a user gesture.
    if (this.ctx.state === 'suspended') await this.ctx.resume();
  }

  /** Ask for 16 kHz directly; browsers that refuse fall back to the default. */
  makeContext(preferSixteen) {
    if (preferSixteen) {
      try {
        return new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      } catch {
        /* resample inside the worklet instead */
      }
    }
    return new AudioContext({ latencyHint: 'interactive' });
  }

  /** stop() can land while start() is awaiting; whatever it made is then ours to drop. */
  throwIfStopped() {
    if (this.stopped) throw new Error('Microphone capture was cancelled.');
  }

  async start() {
    try {
      await this.prepare();
      this.throwIfStopped();

      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      this.throwIfStopped();
      this.stream.getAudioTracks().forEach((track) => {
        track.onended = () => this.onEnded?.();
      });

      let source;
      try {
        source = this.ctx.createMediaStreamSource(this.stream);
      } catch {
        // Firefox builds a 16 kHz context without complaint and then refuses to
        // connect a stream running at the device's rate, so the constructor's
        // fallback never triggers; it has to happen here. The worklet
        // resamples from processorOptions.inputRate.
        await this.ctx.close().catch(() => {});
        this.ctx = this.makeContext(false);
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        this.throwIfStopped();
        source = this.ctx.createMediaStreamSource(this.stream);
      }

      await this.ctx.audioWorklet.addModule('/static/js/worklets/capture-processor.js');
      this.throwIfStopped();

      this.node = new AudioWorkletNode(this.ctx, 'capture-processor', {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: { inputRate: this.ctx.sampleRate },
      });
      this.node.port.onmessage = (event) => this.onFrame(event.data);

      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.75;

      source.connect(this.node);
      source.connect(this.analyser);
      // analyser and worklet are both terminal - nothing reaches the speakers.

      this.pumpLevels();
    } catch (error) {
      // Do not leave a live microphone or an open context behind a failed start.
      await this.stop();
      throw error;
    }
  }

  pumpLevels() {
    const bins = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (!this.analyser) return;
      this.analyser.getByteFrequencyData(bins);
      this.onLevel(bins);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  async stop() {
    this.stopped = true;
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.disconnect();
      this.node = null;
    }
    this.analyser = null;
    if (this.stream) {
      this.stream.getTracks().forEach((track) => {
        track.onended = null;
        track.stop();
      });
      this.stream = null;
    }
    if (this.ctx) {
      const ctx = this.ctx;
      this.ctx = null;
      await ctx.close().catch(() => {});
    }
  }
}
