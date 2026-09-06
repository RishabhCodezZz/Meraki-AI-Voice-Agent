/**
 * Microphone capture worklet.
 *
 * Runs on the audio thread, so it cannot glitch when the main thread is busy
 * rendering — the reason this replaces ScriptProcessorNode. Resamples to
 * 16 kHz when the context could not be opened at that rate, converts to PCM16
 * and posts ~100 ms buffers back to the page.
 */

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 1600; // 100ms at 16kHz

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const inputRate = (options.processorOptions && options.processorOptions.inputRate) || sampleRate;
    this.ratio = inputRate / TARGET_RATE;
    this.needsResample = Math.abs(this.ratio - 1) > 0.01;
    this.buffer = new Int16Array(FRAME_SAMPLES);
    this.filled = 0;
    this.carry = 0; // fractional read position between render quanta
  }

  /** Box-average downsample; cheap and avoids the aliasing of naive picking. */
  resample(input) {
    const out = new Float32Array(Math.ceil((input.length - this.carry) / this.ratio));
    let written = 0;
    let pos = this.carry;

    while (pos < input.length) {
      const start = Math.floor(pos);
      const end = Math.min(Math.floor(pos + this.ratio), input.length);
      let sum = 0;
      let count = 0;
      for (let i = start; i < end; i++) {
        sum += input[i];
        count++;
      }
      out[written++] = count ? sum / count : 0;
      pos += this.ratio;
    }

    this.carry = pos - input.length;
    return out.subarray(0, written);
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    const samples = this.needsResample ? this.resample(channel) : channel;

    for (let i = 0; i < samples.length; i++) {
      const clamped = Math.max(-1, Math.min(1, samples[i]));
      this.buffer[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;

      if (this.filled === FRAME_SAMPLES) {
        // Transfer a copy so the worklet can keep filling its own buffer.
        const frame = this.buffer.slice();
        this.port.postMessage(frame.buffer, [frame.buffer]);
        this.filled = 0;
      }
    }

    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
