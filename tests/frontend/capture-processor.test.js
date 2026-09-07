/**
 * The microphone capture worklet.
 *
 * This is the quietest place in the app to be wrong: a resampler that drifts or
 * a PCM conversion that clips does not throw, it just degrades transcription in
 * a way that looks like the speech model being bad.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// The worklet is a classic script relying on globals the audio thread provides.
let CaptureProcessor;

globalThis.sampleRate = 48000;
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.posted = [];
    this.port = {
      postMessage: (data) => this.posted.push(data),
      onmessage: null,
    };
  }
};
globalThis.registerProcessor = (_name, cls) => {
  CaptureProcessor = cls;
};

await import('../../static/js/worklets/capture-processor.js');

const make = (inputRate) =>
  new CaptureProcessor({ processorOptions: { inputRate } });

/** Frames are posted as transferred ArrayBuffers; read them back as PCM16. */
const framesOf = (proc) => proc.posted.map((buf) => new Int16Array(buf));

const feed = (proc, samples) => proc.process([[Float32Array.from(samples)]]);

test('registers itself under the name the page loads', () => {
  assert.ok(CaptureProcessor, 'registerProcessor was never called');
});

test('resamples 48k down to 16k at a 3:1 ratio', () => {
  const proc = make(48000);
  assert.equal(proc.ratio, 3);
  assert.equal(proc.needsResample, true);

  feed(proc, new Array(4800).fill(0)); // 100ms at 48k -> 1600 at 16k
  const frames = framesOf(proc);
  assert.equal(frames.length, 1, 'one full frame expected');
  assert.equal(frames[0].length, 1600);
});

test('passes 16k straight through without resampling', () => {
  const proc = make(16000);
  assert.equal(proc.needsResample, false);

  feed(proc, new Array(1600).fill(0));
  assert.equal(framesOf(proc).length, 1);
});

test('full scale maps to the PCM16 limits without wrapping', () => {
  const proc = make(16000);
  feed(proc, new Array(1600).fill(1));
  const [frame] = framesOf(proc);
  assert.equal(frame[0], 32767, 'positive full scale');

  const neg = make(16000);
  feed(neg, new Array(1600).fill(-1));
  assert.equal(framesOf(neg)[0][0], -32768, 'negative full scale');
});

test('out-of-range samples clamp rather than wrap', () => {
  // Wrapping would turn a loud peak into the opposite sign - audible as a click
  // and damaging to transcription.
  const proc = make(16000);
  feed(proc, new Array(1600).fill(4.2));
  const [frame] = framesOf(proc);
  assert.equal(frame[0], 32767);
  assert.ok([...frame].every((v) => v === 32767));
});

test('silence stays silent', () => {
  const proc = make(16000);
  feed(proc, new Array(1600).fill(0));
  assert.ok([...framesOf(proc)[0]].every((v) => v === 0));
});

test('a steady tone keeps its amplitude through resampling', () => {
  const proc = make(48000);
  const input = Array.from({ length: 4800 }, () => 0.5);
  feed(proc, input);
  const [frame] = framesOf(proc);
  const expected = Math.round(0.5 * 0x7fff);
  assert.ok(
    [...frame].every((v) => Math.abs(v - expected) <= 1),
    'box-average of a constant must return that constant'
  );
});

test('partial buffers are held until a full frame is ready', () => {
  const proc = make(16000);
  feed(proc, new Array(800).fill(0));
  assert.equal(proc.posted.length, 0, 'half a frame must not be sent');
  feed(proc, new Array(800).fill(0));
  assert.equal(proc.posted.length, 1);
});

test('samples are not lost across process() calls', () => {
  // 48k in, 16k out: 9600 samples in should yield exactly two 1600 frames.
  const proc = make(48000);
  for (let i = 0; i < 25; i++) feed(proc, new Array(384).fill(0));
  assert.equal(framesOf(proc).length, 2);
});

test('a missing input channel does not throw', () => {
  const proc = make(48000);
  assert.equal(proc.process([[]]), true);
  assert.equal(proc.process([]), true);
});

test('process() keeps the node alive', () => {
  const proc = make(16000);
  assert.equal(feed(proc, new Array(128).fill(0)), true);
});
