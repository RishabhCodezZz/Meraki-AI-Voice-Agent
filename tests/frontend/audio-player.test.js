/**
 * Gapless playback scheduling.
 *
 * Chunks arrive while the model is still writing, so they are decoded out of
 * step with each other but must play in order and butt up against one another.
 * A scheduling bug here sounds like stuttering or words in the wrong order, and
 * nothing throws.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// --- browser stubs -----------------------------------------------------------

globalThis.atob = (b64) => Buffer.from(b64, 'base64').toString('binary');
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};

let ctx; // the most recently constructed fake context

class FakeSource {
  constructor(owner) {
    this.owner = owner;
    this.startedAt = null;
    this.stopped = false;
    this.onended = null;
  }
  connect() {}
  start(when) {
    this.startedAt = when;
    this.owner.started.push(this);
  }
  stop() {
    this.stopped = true;
  }
}

class FakeContext {
  constructor() {
    this.currentTime = 0;
    this.state = 'running';
    this.destination = {};
    this.started = [];
    this.pendingDecodes = [];
    ctx = this;
  }
  async resume() {
    this.state = 'running';
  }
  createGain() {
    return { connect() {}, gain: { value: 1 } };
  }
  createAnalyser() {
    return {
      fftSize: 0,
      smoothingTimeConstant: 0,
      frequencyBinCount: 8,
      connect() {},
      getByteFrequencyData() {},
    };
  }
  createBufferSource() {
    return new FakeSource(this);
  }
  // One byte of input == one second of audio, so tests can state durations.
  decodeAudioData(arrayBuffer) {
    const duration = arrayBuffer.byteLength;
    if (duration === 0) return Promise.reject(new Error('bad audio'));
    return Promise.resolve({ duration });
  }
  async close() {}
}

globalThis.AudioContext = FakeContext;

const { SpeechPlayer } = await import('../../static/js/audio-player.js');

/** base64 for n bytes, i.e. n seconds of fake audio. */
const audio = (seconds) => Buffer.alloc(seconds, 1).toString('base64');

const newPlayer = () => new SpeechPlayer({ onLevel: () => {}, onIdle: () => {} });

// --- ordering ----------------------------------------------------------------

test('the first chunk starts almost immediately', async () => {
  const player = newPlayer();
  await player.enqueue(audio(2));

  const [first] = ctx.started;
  assert.ok(first.startedAt >= ctx.currentTime, 'must not be scheduled in the past');
  assert.ok(first.startedAt < 0.2, `started at ${first.startedAt}, expected a small lead`);
});

test('chunks are scheduled back to back with no gap', async () => {
  const player = newPlayer();
  await player.enqueue(audio(2));
  await player.enqueue(audio(3));
  await player.enqueue(audio(1));

  const [a, b, c] = ctx.started;
  assert.equal(b.startedAt, a.startedAt + 2, 'second follows the first exactly');
  assert.equal(c.startedAt, b.startedAt + 3, 'third follows the second exactly');
});

test('a late chunk does not overlap what is already playing', async () => {
  const player = newPlayer();
  await player.enqueue(audio(5));
  ctx.currentTime = 1; // one second in
  await player.enqueue(audio(2));

  const [a, b] = ctx.started;
  assert.equal(b.startedAt, a.startedAt + 5, 'queued after, not on top of');
});

// --- barge-in ----------------------------------------------------------------

test('flush stops everything that was scheduled', async () => {
  const player = newPlayer();
  await player.enqueue(audio(4));
  await player.enqueue(audio(4));

  player.flush();

  assert.ok(ctx.started.every((s) => s.stopped), 'all sources stopped');
  assert.equal(player.playing, false);
});

test('audio still decoding when the user interrupts is discarded', async () => {
  // This is the subtle one: decode finishes after flush, and without the
  // generation guard the interrupted reply would start speaking anyway.
  const player = newPlayer();
  await player.enqueue(audio(2));
  const before = ctx.started.length;

  const inFlight = player.enqueue(audio(2));
  player.flush();
  await inFlight;

  assert.equal(ctx.started.length, before, 'nothing new was scheduled after flush');
});

test('playback resumes cleanly after a flush', async () => {
  const player = newPlayer();
  await player.enqueue(audio(3));
  player.flush();
  await player.enqueue(audio(2));

  const last = ctx.started[ctx.started.length - 1];
  assert.ok(last.startedAt < 0.2, 'the next reply starts fresh, not queued behind');
});

// --- failure -----------------------------------------------------------------

test('an undecodable chunk is dropped without killing the reply', async () => {
  const player = newPlayer();
  await player.enqueue(audio(2));
  await player.enqueue(''); // decodeAudioData rejects
  await player.enqueue(audio(2));

  assert.equal(ctx.started.length, 2, 'the good chunks still played');
});

test('a dropped chunk does not leave a hole in the timeline', async () => {
  const player = newPlayer();
  await player.enqueue(audio(2));
  await player.enqueue('');
  await player.enqueue(audio(3));

  const [a, b] = ctx.started;
  assert.equal(b.startedAt, a.startedAt + 2, 'still contiguous');
});

// --- lifecycle ---------------------------------------------------------------

test('reports idle only once every source has finished', async () => {
  let idle = 0;
  const player = new SpeechPlayer({ onLevel: () => {}, onIdle: () => idle++ });
  await player.enqueue(audio(1));
  await player.enqueue(audio(1));

  const [a, b] = ctx.started;
  a.onended();
  assert.equal(idle, 0, 'still one source outstanding');
  b.onended();
  assert.equal(idle, 1);
  assert.equal(player.playing, false);
});

test('a suspended context is resumed before scheduling', async () => {
  const player = newPlayer();
  const context = await player.ensureContext();
  context.state = 'suspended';
  await player.ensureContext();
  assert.equal(context.state, 'running');
});
