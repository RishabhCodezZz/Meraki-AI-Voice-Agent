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
    // When true, decodes stay pending until the test settles them, so a test
    // can make decodes finish in any order it likes.
    this.manualDecode = false;
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
    if (!this.manualDecode) return Promise.resolve({ duration });
    return new Promise((resolve, reject) => {
      this.pendingDecodes.push({
        duration,
        resolve: () => resolve({ duration }),
        reject: () => reject(new Error('bad audio')),
      });
    });
  }
  async close() {}
}

globalThis.AudioContext = FakeContext;

const { SpeechPlayer } = await import('../../static/js/audio-player.js');

/** base64 for n bytes, i.e. n seconds of fake audio. */
const audio = (seconds) => Buffer.alloc(seconds, 1).toString('base64');

const newPlayer = () => new SpeechPlayer({ onLevel: () => {}, onIdle: () => {} });

/** A player whose decodes only finish when the test says so. */
async function manualPlayer(onIdle = () => {}) {
  const player = new SpeechPlayer({ onLevel: () => {}, onIdle });
  await player.ensureContext();
  ctx.manualDecode = true;
  return player;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

/** Settle the pending decode of that length, if the player has asked for it. */
function settleDecode(seconds, how = 'resolve') {
  const at = ctx.pendingDecodes.findIndex((d) => d.duration === seconds);
  if (at === -1) return false;
  const [decode] = ctx.pendingDecodes.splice(at, 1);
  decode[how]();
  return true;
}

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

test('chunks are scheduled in arrival order even when a later decode finishes first', async () => {
  const player = await manualPlayer();
  const a = player.enqueue(audio(3));
  const b = player.enqueue(audio(1));

  // Finish B first. A serialised player has not even asked for B yet, which is
  // the point: whichever order the decodes land in, A must play first.
  await tick();
  settleDecode(1);
  await tick();
  settleDecode(3);
  await tick();
  settleDecode(1);
  await Promise.all([a, b]);

  assert.equal(ctx.started.length, 2);
  const [first, second] = ctx.started;
  assert.equal(first.buffer.duration, 3, 'A, the 3 s chunk, plays first');
  assert.equal(second.startedAt, first.startedAt + 3, 'B follows A exactly');
});

test('playing is true while a chunk is still decoding', async () => {
  const player = await manualPlayer();
  assert.equal(player.playing, false);

  const queued = player.enqueue(audio(2));
  assert.equal(player.pending, 1, 'counted synchronously, before any await');
  assert.equal(player.playing, true, 'speech_done must not see a quiet player');

  await tick();
  settleDecode(2);
  await queued;

  assert.equal(player.pending, 0);
  assert.equal(player.playing, true, 'now held by the scheduled source');
});

test('flush discards chunks that are still queued behind a decode', async () => {
  const player = await manualPlayer();
  const a = player.enqueue(audio(2));
  const b = player.enqueue(audio(2));
  await tick();

  player.flush();
  settleDecode(2);
  await tick();
  assert.equal(ctx.pendingDecodes.length, 0, 'the stale chunk was skipped, not decoded');
  await Promise.all([a, b]);

  assert.equal(ctx.started.length, 0, 'nothing from before the flush plays');
  assert.equal(player.pending, 0);
  assert.equal(player.playing, false);
});

test('a failed decode does not block the chunks behind it', async () => {
  const player = await manualPlayer();
  const a = player.enqueue(audio(1));
  const b = player.enqueue(audio(2));
  const c = player.enqueue(audio(3));

  await tick();
  settleDecode(1);
  await tick();
  settleDecode(2, 'reject');
  await tick();
  settleDecode(3);
  await Promise.all([a, b, c]);

  assert.equal(ctx.started.length, 2, 'A and C played');
  const [first, third] = ctx.started;
  assert.equal(third.buffer.duration, 3);
  assert.equal(third.startedAt, first.startedAt + 1, 'the dropped chunk leaves no hole');
  assert.equal(player.pending, 0);
});

test('a chunk that throws before decoding does not wedge the chain', async () => {
  // enqueue() itself can reject (atob throws on bad base64); the next call must
  // still run. The stub's atob throws on null, as the real one does on garbage.
  const player = newPlayer();
  await assert.rejects(player.enqueue(null));
  await player.enqueue(audio(1));
  assert.equal(ctx.started.length, 1);
  assert.equal(player.pending, 0);
});

test('a reply queued right after a flush starts fresh', async () => {
  const player = await manualPlayer();
  const first = player.enqueue(audio(2));
  await tick();
  settleDecode(2);
  await first;

  player.flush(); // barge-in
  const second = player.enqueue(audio(1));
  await tick();
  settleDecode(1);
  await second;

  const last = ctx.started[ctx.started.length - 1];
  assert.ok(last.startedAt < 0.2, 'the new reply starts fresh');
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

test('does not report idle in the gap between chunks of one reply', async () => {
  let idle = 0;
  const player = await manualPlayer(() => idle++);
  const a = player.enqueue(audio(1));
  const b = player.enqueue(audio(1));

  await tick();
  settleDecode(1);
  await a;
  ctx.started[0].onended(); // A finishes while B is still decoding
  assert.equal(idle, 0, 'B is on its way');
  assert.equal(player.playing, true);

  await tick();
  settleDecode(1);
  await b;
  ctx.started[1].onended();
  assert.equal(idle, 1);
});

test('reports idle when the last chunk is dropped after earlier audio has finished', async () => {
  // Nothing is left to fire onended, so the player has to say it itself or the
  // UI would wait on a reply that is already over.
  let idle = 0;
  const player = await manualPlayer(() => idle++);
  const a = player.enqueue(audio(1));
  const b = player.enqueue(audio(2));

  await tick();
  settleDecode(1);
  await a;
  ctx.started[0].onended();
  assert.equal(idle, 0);

  await tick();
  settleDecode(2, 'reject');
  await b;
  assert.equal(idle, 1);
  assert.equal(player.playing, false);
});

test('flush does not report idle', async () => {
  let idle = 0;
  const player = await manualPlayer(() => idle++);
  const a = player.enqueue(audio(1));
  await tick();
  player.flush();
  settleDecode(1);
  await a;
  assert.equal(idle, 0, 'the interrupt already told the UI what state it is in');
});

test('a suspended context is resumed before scheduling', async () => {
  const player = newPlayer();
  const context = await player.ensureContext();
  context.state = 'suspended';
  await player.ensureContext();
  assert.equal(context.state, 'running');
});
