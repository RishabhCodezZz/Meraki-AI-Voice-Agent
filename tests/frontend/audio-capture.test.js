/**
 * MicCapture's start-up and clean-up paths. The happy path is hard to get
 * wrong; these are the ones that leave a live microphone or a stuck session
 * behind when they are: Firefox's sample-rate refusal, a start that fails
 * part-way, and a stop that lands while the permission prompt is open.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};

let contexts; // every context built, in order
let stream; // the most recent fake MediaStream
let behaviour; // knobs the tests turn

class FakeTrack {
  constructor() {
    this.stopped = false;
    this.onended = null;
  }
  stop() {
    this.stopped = true;
  }
}

class FakeContext {
  constructor(options = {}) {
    this.options = options;
    this.sampleRate = options.sampleRate || 48000;
    this.state = 'running';
    this.closed = false;
    this.audioWorklet = { addModule: async () => behaviour.addModule?.() };
    contexts.push(this);
    if (options.sampleRate && behaviour.rejectSampleRate) {
      throw new Error('sample rate not supported');
    }
  }
  async resume() {
    this.state = 'running';
  }
  async close() {
    this.closed = true;
  }
  createMediaStreamSource() {
    // Firefox: a context at a fixed rate will not take the device's stream.
    if (behaviour.refuseFixedRateSource && this.options.sampleRate) {
      throw new Error('different sample-rate');
    }
    return { connect() {} };
  }
  createAnalyser() {
    return { fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 4, getByteFrequencyData() {} };
  }
}

class FakeWorkletNode {
  constructor() {
    this.port = {};
  }
  disconnect() {}
}

globalThis.AudioContext = FakeContext;
globalThis.AudioWorkletNode = FakeWorkletNode;
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    mediaDevices: {
      getUserMedia: async () => {
        await behaviour.beforeStream?.();
        stream = { track: new FakeTrack() };
        stream.getTracks = () => [stream.track];
        stream.getAudioTracks = () => [stream.track];
        return stream;
      },
    },
  },
});

const { MicCapture } = await import('../../static/js/audio-capture.js');

const newCapture = (extra = {}) =>
  new MicCapture({ onFrame: () => {}, onLevel: () => {}, ...extra });

test.beforeEach(() => {
  contexts = [];
  stream = null;
  behaviour = {};
});

test('prepare builds the context before its first await', () => {
  const capture = newCapture();
  const pending = capture.prepare();
  assert.equal(contexts.length, 1, 'made synchronously, inside the click gesture');
  assert.equal(contexts[0].options.sampleRate, 16000);
  return pending;
});

test('start reuses the context prepare made', async () => {
  const capture = newCapture();
  await capture.prepare();
  await capture.start();
  assert.equal(contexts.length, 1);
  assert.equal(capture.active, true);
});

test('a refused 16 kHz source is retried on a default-rate context', async () => {
  behaviour.refuseFixedRateSource = true;
  const capture = newCapture();
  await capture.start();

  assert.equal(contexts.length, 2);
  assert.equal(contexts[0].closed, true, 'the context that could not connect is closed');
  assert.equal(contexts[1].options.sampleRate, undefined);
  assert.equal(capture.ctx, contexts[1]);
  assert.equal(capture.active, true);
});

test('a constructor that rejects 16 kHz falls back to the default rate', async () => {
  behaviour.rejectSampleRate = true;
  const capture = newCapture();
  await capture.start();
  assert.equal(capture.active, true);
  assert.equal(capture.ctx.options.sampleRate, undefined);
});

test('a start that fails part-way releases the stream and the context', async () => {
  behaviour.addModule = () => {
    throw new Error('worklet failed to load');
  };
  const capture = newCapture();
  await assert.rejects(capture.start(), { message: 'worklet failed to load' });

  assert.equal(stream.track.stopped, true, 'the microphone is off again');
  assert.equal(contexts[0].closed, true);
  assert.equal(capture.active, false);
});

test('a stop during the permission prompt drops the stream that arrives after it', async () => {
  let answer;
  behaviour.beforeStream = () => new Promise((resolve) => (answer = resolve));
  const capture = newCapture();
  const starting = capture.start();
  await new Promise((resolve) => setImmediate(resolve));

  await capture.stop();
  answer(); // the visitor clicks Allow after Meraki has already stopped
  await assert.rejects(starting, { message: 'Microphone capture was cancelled.' });

  assert.equal(stream.track.stopped, true, 'the late stream is not left running');
  assert.equal(contexts[0].closed, true);
  assert.equal(capture.active, false);
});

test('the microphone going away is reported, a deliberate stop is not', async () => {
  let ended = 0;
  const capture = newCapture({ onEnded: () => ended++ });
  await capture.start();

  stream.track.onended(); // unplugged
  assert.equal(ended, 1);

  const { track } = stream;
  await capture.stop();
  assert.equal(track.onended, null, 'stop() detaches the listener');
});
