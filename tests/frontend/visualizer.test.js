/**
 * The bar meter's pure parts: what the idle meter targets, what it ignores, and
 * how often it touches the stylesheet.
 *
 * The canvas is stubbed to a recorder; none of this needs pixels. What matters
 * is that the idle meter stays readable as a meter, that live data cannot leak
 * into it, and that drawing does not ask the browser to resolve CSS every frame.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// --- browser stubs -----------------------------------------------------------

let styleReads = 0;
let reducedMotion = false;
let frames = []; // requestAnimationFrame callbacks waiting to run
let listeners = {}; // document listeners by event name
let draws = 0;

const ctxStub = {
  setTransform() {},
  clearRect() { draws += 1; },
  beginPath() {},
  fill() {},
  fillRect() {},
  fillStyle: '',
  globalAlpha: 1,
};

const canvasStub = {
  width: 0,
  height: 0,
  getContext: () => ctxStub,
  getBoundingClientRect: () => ({ width: 620, height: 82 }),
};

globalThis.window = { addEventListener() {}, removeEventListener() {}, devicePixelRatio: 1 };
globalThis.document = {
  hidden: false,
  documentElement: {},
  addEventListener(type, fn) { listeners[type] = fn; },
  removeEventListener() {},
};
globalThis.getComputedStyle = () => {
  styleReads += 1;
  return { getPropertyValue: (name) => (name === '--accent' ? ' #e8434a ' : ' #33414a ') };
};
globalThis.matchMedia = (query) => ({
  matches: query.includes('reduce') ? reducedMotion : false,
  addEventListener() {},
  removeEventListener() {},
});
globalThis.requestAnimationFrame = (fn) => frames.push(fn);

const { Visualizer } = await import('../../static/js/visualizer.js');

function make({ reduced = false } = {}) {
  reducedMotion = reduced;
  frames = [];
  listeners = {};
  draws = 0;
  document.hidden = false;
  styleReads = 0;
  return new Visualizer(canvasStub);
}

// --- tests -------------------------------------------------------------------

test('idle targets never fall below the floor, on any bar, at any time', () => {
  const viz = make();
  for (let frame = 0; frame < 600; frame++) {
    viz.step();
    for (const target of viz.targets) {
      assert.ok(target >= Visualizer.idleFloor, `target ${target} under floor`);
    }
  }
  // A literal, not Visualizer.idleFloor: that is derived from the same
  // constants, so asserting against it could never fail.
  assert.ok(Visualizer.idleFloor >= 0.08);
  for (const target of viz.targets) assert.ok(target >= 0.08, `${target} reads as a dotted rule`);
});

test('blocked and connecting are drawn like idle, not left on stale targets', () => {
  for (const state of ['blocked', 'connecting']) {
    const viz = make();
    viz.targets.fill(0.9); // leftover from a listening session
    viz.setState(state);
    viz.step();
    for (const target of viz.targets) {
      assert.ok(target < 0.2, `${state} kept stale target ${target}`);
      assert.ok(target >= Visualizer.idleFloor);
    }
  }
});

test('setSpectrum is ignored while idle', () => {
  const viz = make();
  viz.setState('idle');
  viz.step();
  const before = Array.from(viz.targets);
  viz.setSpectrum(new Uint8Array(128).fill(255));
  assert.deepEqual(Array.from(viz.targets), before);
});

test('setSpectrum moves the targets while listening', () => {
  const viz = make();
  viz.setState('listening');
  viz.setSpectrum(new Uint8Array(128).fill(255));
  assert.ok(viz.targets.every((t) => t > 0.9));
});

test('colours are read once per refreshColors, not once per draw', () => {
  const viz = make();
  assert.equal(styleReads, 1, 'constructor reads the palette once');

  for (let i = 0; i < 10; i++) viz.draw();
  assert.equal(styleReads, 1, 'draw must not touch getComputedStyle');

  viz.refreshColors();
  assert.equal(styleReads, 2);
  assert.equal(viz.accent, '#e8434a', 'values are trimmed');
  assert.equal(viz.dim, '#33414a');
});

test('with reduced motion the idle targets are constant across steps', () => {
  const viz = make({ reduced: true });
  viz.step();
  const first = Array.from(viz.targets);
  for (let i = 0; i < 50; i++) viz.step();
  assert.deepEqual(Array.from(viz.targets), first);
  assert.ok(first.every((t) => t >= Visualizer.idleFloor));
});

test('with reduced motion the thinking targets are constant across steps', () => {
  const viz = make({ reduced: true });
  viz.setState('thinking');
  viz.step();
  const first = Array.from(viz.targets);
  for (let i = 0; i < 50; i++) viz.step();
  assert.deepEqual(Array.from(viz.targets), first);
});

test('without reduced motion the idle targets do move', () => {
  const viz = make();
  viz.step();
  const first = Array.from(viz.targets);
  for (let i = 0; i < 50; i++) viz.step();
  assert.notDeepEqual(Array.from(viz.targets), first);
});

test('reduced motion skips redraws once the bars have settled', () => {
  const viz = make({ reduced: true });
  for (let i = 0; i < 200; i++) viz.step(); // let the easing finish
  viz.dirty = false;
  assert.equal(viz.shouldDraw(), false);

  viz.setState('thinking');
  assert.equal(viz.shouldDraw(), true, 'a state change must repaint');
});

/** Run the one pending frame, if any; returns whether there was one. */
function tick() {
  const next = frames.shift();
  if (!next) return false;
  next();
  return true;
}

test('start() twice leaves exactly one frame in flight', () => {
  const viz = make();
  viz.start();
  viz.start();
  assert.equal(frames.length, 1);
  tick();
  assert.equal(frames.length, 1, 'each frame schedules exactly one successor');
  viz.stop();
});

test('a visibilitychange while a frame is pending does not queue a second loop', () => {
  const viz = make();
  viz.start();
  listeners.visibilitychange(); // tab shown again; a frame is already queued
  listeners.visibilitychange();
  assert.equal(frames.length, 1);
  viz.stop();
});

test('the loop lapses while the tab is hidden and resumes on visibilitychange', () => {
  const viz = make();
  viz.start();
  tick(); // a normal frame

  document.hidden = true;
  tick(); // the frame already queued runs, then must not reschedule
  assert.equal(frames.length, 0, 'hidden tab must not keep requesting frames');

  document.hidden = false;
  listeners.visibilitychange();
  assert.equal(frames.length, 1, 'becoming visible restarts the loop');
  viz.stop();
});

test('with reduced motion the loop stops painting once nothing has changed', () => {
  const viz = make({ reduced: true });
  viz.start();
  for (let i = 0; i < 60; i++) tick(); // easing settles, last paint happens
  draws = 0;
  for (let i = 0; i < 20; i++) tick();
  assert.equal(draws, 0, 'idle frames must not repaint');

  viz.setState('thinking');
  tick();
  assert.equal(draws, 1, 'a state change repaints');
  viz.stop();
});

test('without reduced motion every frame paints', () => {
  const viz = make();
  viz.start();
  draws = 0;
  for (let i = 0; i < 5; i++) tick();
  assert.equal(draws, 5);
  viz.stop();
});
