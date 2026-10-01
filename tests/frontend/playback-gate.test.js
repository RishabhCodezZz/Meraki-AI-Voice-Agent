/**
 * The speaking state has two inputs that arrive in either order: the server
 * saying the turn's audio has all been sent, and the player running dry. The
 * UI may only leave "Speaking" once both are true.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { PlaybackGate } from '../../static/js/playback-gate.js';

function rig() {
  const state = { playing: false, settled: 0 };
  const gate = new PlaybackGate({
    isPlaying: () => state.playing,
    onSettled: () => state.settled++,
  });
  return { state, gate };
}

test('settles only when the turn has ended and the player is quiet', () => {
  const { state, gate } = rig();
  gate.turnStarted();
  state.playing = true;
  gate.turnEnded();
  assert.equal(state.settled, 0, 'audio is still playing');

  state.playing = false;
  gate.playerIdle();
  assert.equal(state.settled, 1);
});

test('the player going idle mid-turn does not settle', () => {
  // The gap between chunks of one reply looks exactly like this.
  const { state, gate } = rig();
  gate.turnStarted();
  gate.playerIdle();
  assert.equal(state.settled, 0);
});

test('a turn that ends while the player is already quiet settles at once', () => {
  const { state, gate } = rig();
  gate.turnStarted();
  gate.turnEnded();
  assert.equal(state.settled, 1);
});

test('a turn ending while audio plays waits for the player', () => {
  const { state, gate } = rig();
  gate.turnStarted();
  state.playing = true;
  gate.turnEnded();
  gate.turnEnded(); // a duplicate must not slip through either
  assert.equal(state.settled, 0);

  state.playing = false;
  gate.playerIdle();
  assert.equal(state.settled, 1);
});

test('cancel suppresses settlement for the interrupted turn', () => {
  const { state, gate } = rig();
  gate.turnStarted();
  state.playing = true;
  gate.cancel();
  state.playing = false;
  gate.playerIdle();
  assert.equal(state.settled, 0, 'the interrupt set the state itself');
});

test('two turns in a row each settle once', () => {
  const { state, gate } = rig();
  for (let turn = 1; turn <= 2; turn++) {
    gate.turnStarted();
    state.playing = true;
    gate.turnEnded();
    state.playing = false;
    gate.playerIdle();
    assert.equal(state.settled, turn);
  }
});

test('an idle signal with no finished turn behind it settles nothing', () => {
  // After settling, or after an interrupt, the player can still report idle.
  const { state, gate } = rig();
  gate.turnStarted();
  gate.turnEnded();
  assert.equal(state.settled, 1);

  gate.playerIdle();
  assert.equal(state.settled, 1, 'the turn already settled');
});

test('a second turnEnded after settling does not settle again', () => {
  // A failed synthesis sends error(tts) and then speech_done for the same turn.
  // Both end the turn, and the UI must only be told once.
  const { state, gate } = rig();
  gate.turnStarted();
  gate.turnEnded();
  gate.turnEnded();
  assert.equal(state.settled, 1);
});
