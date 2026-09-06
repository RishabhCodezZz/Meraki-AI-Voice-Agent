/**
 * Microphone capture: getUserMedia -> AudioWorklet -> PCM16 frames.
 *
 * The mic is deliberately never connected to the destination. The old build
 * routed it to the speakers, which fed the assistant's own voice back into the
 * transcriber.
 */

export class MicCapture {
  constructor({ onFrame, onLevel }) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.analyser = null;
    this.raf = 0;
  }

  get active() {
    return Boolean(this.node);
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    // Ask for 16 kHz directly; browsers that refuse fall back to resampling
    // inside the worklet.
    try {
      this.ctx = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
    } catch {
      this.ctx = new AudioContext({ latencyHint: 'interactive' });
    }

    // Safari and iOS start contexts suspended until a user gesture.
    if (this.ctx.state === 'suspended') await this.ctx.resume();

    await this.ctx.audioWorklet.addModule('/static/js/worklets/capture-processor.js');

    const source = this.ctx.createMediaStreamSource(this.stream);
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

  setMuted(muted) {
    if (this.node) this.node.port.postMessage({ type: 'mute', value: muted });
  }

  async stop() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.disconnect();
      this.node = null;
    }
    this.analyser = null;
    if (this.stream) {
      this.stream.getTracks().forEach((track) => track.stop());
      this.stream = null;
    }
    if (this.ctx) {
      await this.ctx.close().catch(() => {});
      this.ctx = null;
    }
  }
}
