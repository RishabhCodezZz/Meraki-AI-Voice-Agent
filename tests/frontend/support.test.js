/**
 * Pure helpers behind the page's start-up checks. They live apart from app.js
 * so the wording a visitor sees, and the timer handling that keeps a stalled
 * connection from hanging the mic button, can be tested without a browser.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  detectSupport,
  describeStartError,
  isMicBlocked,
  MIC_BLOCKED_ADVICE,
  endsTurn,
  TURN_ENDING_ERROR_CODES,
  withTimeout,
  makeSessionId,
  isValidSessionId,
  pickKeys,
  networkError,
  shouldRetryConnect,
  retryWithin,
  CONNECT_RETRY_DELAY_MS,
  CONNECT_BUDGET_MS,
} from '../../static/js/support.js';

/** An environment that has everything; tests knock items out of it. */
function fullEnv(overrides = {}) {
  return {
    isSecureContext: true,
    navigator: { mediaDevices: { getUserMedia() {} } },
    AudioContext: class {},
    AudioWorkletNode: class {},
    WebSocket: class {},
    ...overrides,
  };
}

// --- detectSupport -----------------------------------------------------------

test('a complete environment is supported', () => {
  assert.deepEqual(detectSupport(fullEnv()), { ok: true });
});

test('an insecure page is reported first, whatever else is missing', () => {
  const result = detectSupport(fullEnv({ isSecureContext: false, WebSocket: undefined }));
  assert.equal(result.ok, false);
  assert.equal(
    result.reason,
    'Microphone access needs a secure connection (https or localhost).'
  );
});

test('missing getUserMedia means the browser cannot capture audio', () => {
  for (const navigator of [undefined, {}, { mediaDevices: {} }]) {
    const result = detectSupport(fullEnv({ navigator }));
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'This browser cannot capture audio.');
  }
});

test('a missing audio context is reported, and webkitAudioContext counts', () => {
  const none = detectSupport(fullEnv({ AudioContext: undefined }));
  assert.equal(none.ok, false);
  assert.equal(none.reason, 'This browser does not support the audio features Meraki needs.');

  const prefixed = detectSupport(
    fullEnv({ AudioContext: undefined, webkitAudioContext: class {} })
  );
  assert.deepEqual(prefixed, { ok: true });
});

test('a missing AudioWorkletNode is reported', () => {
  const result = detectSupport(fullEnv({ AudioWorkletNode: undefined }));
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'This browser does not support the audio features Meraki needs.');
});

test('a missing WebSocket is reported', () => {
  const result = detectSupport(fullEnv({ WebSocket: undefined }));
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'This browser does not support the audio features Meraki needs.');
});

// --- describeStartError ------------------------------------------------------

test('known getUserMedia failures read as plain language', () => {
  const cases = {
    NotAllowedError: 'Microphone access was blocked.',
    SecurityError: 'Microphone access was blocked.',
    NotFoundError: 'No microphone was found.',
    OverconstrainedError: 'No microphone was found.',
    NotReadableError: 'The microphone is busy or unavailable.',
  };
  for (const [name, text] of Object.entries(cases)) {
    assert.equal(describeStartError({ name, message: 'raw browser text' }), text, name);
  }
});

test('other errors keep their message, with a fallback when there is none', () => {
  assert.equal(describeStartError(new Error('The server closed the connection.')),
    'The server closed the connection.');
  assert.equal(describeStartError(new Error('')), 'Could not start.');
  assert.equal(describeStartError(undefined), 'Could not start.');
  assert.equal(describeStartError(null), 'Could not start.');
});

// --- isMicBlocked ------------------------------------------------------------

test('only a denied microphone counts as blocked, which Retry cannot fix', () => {
  assert.equal(isMicBlocked({ name: 'NotAllowedError' }), true);
  assert.equal(isMicBlocked({ name: 'SecurityError' }), true);
  // These can clear up by themselves (plug it in, close the other app).
  for (const name of ['NotFoundError', 'NotReadableError', 'OverconstrainedError', 'Error']) {
    assert.equal(isMicBlocked({ name }), false, name);
  }
  assert.equal(isMicBlocked(new Error('The server closed the connection.')), false);
  assert.equal(isMicBlocked(undefined), false);
  assert.equal(isMicBlocked(null), false);
});

test('the advice for a blocked microphone says where to fix it and what to press', () => {
  assert.match(MIC_BLOCKED_ADVICE, /site settings/);
  assert.match(MIC_BLOCKED_ADVICE, /Start talking/);
});

// --- endsTurn ----------------------------------------------------------------

test('errors from the model, the voice, the network or a crash end the turn', () => {
  for (const code of ['llm', 'tts', 'network', 'internal']) {
    assert.equal(endsTurn(code), true, code);
  }
});

test('a transcription error does not end a turn that is still being spoken', () => {
  // Deepgram reports a problem while a reply is mid-flight; the reply goes on.
  assert.equal(endsTurn('stt'), false);
});

test('unknown, missing or inherited codes never end a turn', () => {
  for (const code of ['keys', 'handshake', 'something-new', '', undefined, null, 42,
    '__proto__', 'constructor', 'toString']) {
    assert.equal(endsTurn(code), false, String(code));
  }
});

test('the set of turn-ending codes is exactly the four the pipeline sends', () => {
  assert.deepEqual([...TURN_ENDING_ERROR_CODES].sort(), ['internal', 'llm', 'network', 'tts']);
});

// --- withTimeout -------------------------------------------------------------

/** Replace the timer functions with ones that record what is still armed. */
function trackTimers(t) {
  const armed = new Set();
  const realSet = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  globalThis.setTimeout = (fn, ms) => {
    const handle = realSet(() => {
      armed.delete(handle);
      fn();
    }, ms);
    armed.add(handle);
    return handle;
  };
  globalThis.clearTimeout = (handle) => {
    armed.delete(handle);
    realClear(handle);
  };
  t.after(() => {
    globalThis.setTimeout = realSet;
    globalThis.clearTimeout = realClear;
  });
  return armed;
}

test('withTimeout resolves with the value when the promise wins', async (t) => {
  const armed = trackTimers(t);
  assert.equal(await withTimeout(Promise.resolve('ready'), 20, 'too slow'), 'ready');
  assert.equal(armed.size, 0, 'the timer was cleared, not left to fire later');
});

test('withTimeout passes the promise\'s own rejection through and clears the timer', async (t) => {
  const armed = trackTimers(t);
  await assert.rejects(
    withTimeout(Promise.reject(new Error('refused')), 20, 'too slow'),
    { message: 'refused' }
  );
  assert.equal(armed.size, 0);
});

test('withTimeout rejects with the given message when the promise is too slow', async (t) => {
  const armed = trackTimers(t);
  const never = new Promise(() => {});
  await assert.rejects(withTimeout(never, 20, 'too slow'), { message: 'too slow' });
  assert.equal(armed.size, 0);
});

// --- makeSessionId -----------------------------------------------------------

const ID_SHAPE = /^[A-Za-z0-9_-]{16,32}$/;

test('session ids are 16-32 characters from the server\'s alphabet, in every environment', () => {
  const envs = {
    'randomUUID': { crypto: { randomUUID: () => '123e4567-e89b-42d3-a456-426614174000' } },
    'getRandomValues only': {
      crypto: { getRandomValues: (a) => a.fill(7) },
    },
    'no crypto': {},
    'crypto that throws': {
      crypto: {
        randomUUID() { throw new Error('nope'); },
        getRandomValues() { throw new Error('nope'); },
      },
    },
  };
  for (const [name, env] of Object.entries(envs)) {
    for (let i = 0; i < 50; i++) {
      assert.match(makeSessionId(env), ID_SHAPE, name);
    }
  }
});

test('session ids stay long enough when Math.random is unlucky', () => {
  // Math.random().toString(36).slice(2) is only "0" for a draw of exactly 0 and
  // can be a handful of characters for short expansions; the id must not care.
  const real = Math.random;
  try {
    Math.random = () => 0;
    assert.match(makeSessionId({}), ID_SHAPE);
    Math.random = () => 0.5;
    assert.match(makeSessionId({}), ID_SHAPE);
  } finally {
    Math.random = real;
  }
});

test('two session ids differ, with and without crypto', () => {
  assert.notEqual(makeSessionId({}), makeSessionId({}));
  const withCrypto = { crypto: globalThis.crypto };
  assert.notEqual(makeSessionId(withCrypto), makeSessionId(withCrypto));
});

test('the default environment works', () => {
  assert.match(makeSessionId(), ID_SHAPE);
});

// --- pickKeys ----------------------------------------------------------------

test('pickKeys sends only the named fields that hold a non-empty string', () => {
  const fields = ['deepgram', 'ollama', 'murf'];
  const stored = Object.create({ murf: 'inherited' });
  Object.assign(stored, { deepgram: 'a', ollama: '', extra: 'x' });
  assert.deepEqual(pickKeys(stored, fields), { deepgram: 'a' });
  assert.deepEqual(pickKeys({ deepgram: 5 }, fields), {});
});

test('pickKeys tolerates stored junk', () => {
  for (const junk of [null, undefined, 'text', 42, []]) {
    assert.deepEqual(pickKeys(junk, ['deepgram']), {});
  }
});

// --- isValidSessionId --------------------------------------------------------

test('a session id is valid exactly when the server would keep it', () => {
  for (const id of ['abcd1234', 'A_b-C_d-', 'x'.repeat(64), makeSessionId()]) {
    assert.equal(isValidSessionId(id), true, id);
  }
  const bad = [
    '', 'short', 'x'.repeat(7), 'x'.repeat(65),
    'has space1', 'dots.in.it1', 'slash/slash1', 'abcd1234\n', '../../etc/passwd',
    null, undefined, 12345678, {}, ['abcd1234'],
  ];
  for (const id of bad) assert.equal(isValidSessionId(id), false, String(id));
});

// --- shouldRetryConnect ------------------------------------------------------

test('only network-level failures are worth retrying', () => {
  assert.equal(shouldRetryConnect(networkError('Could not reach the server.')), true);
  assert.equal(shouldRetryConnect(networkError('The server closed the connection.')), true);
});

test('a server error frame, a support failure or anything unknown is not retried', () => {
  // The same wording as a network failure must not be enough: a server frame
  // that happens to read "Could not reach the server." is still the server's.
  assert.equal(shouldRetryConnect(new Error('Could not reach the server.')), false);
  assert.equal(shouldRetryConnect(new Error('Deepgram rejected that key.')), false);
  assert.equal(shouldRetryConnect(Object.assign(new Error('x'), { name: 'NotAllowedError' })), false);
  assert.equal(shouldRetryConnect(undefined), false);
  assert.equal(shouldRetryConnect(null), false);
  assert.equal(shouldRetryConnect('Could not reach the server.'), false);
});

test('withTimeout can reject with an error the caller built', async (t) => {
  trackTimers(t);
  const never = new Promise(() => {});
  const error = await withTimeout(never, 20, 'too slow', networkError).catch((e) => e);
  assert.equal(error.message, 'too slow');
  assert.equal(shouldRetryConnect(error), true, 'a timeout is a network failure');
});

// --- retryWithin -------------------------------------------------------------

/**
 * A clock the test owns: `sleep` moves it forward instead of waiting, so the
 * arithmetic of a 60 s budget runs instantly. `took` lets an attempt spend time.
 */
function fakeClock() {
  const clock = {
    t: 0,
    sleeps: [],
    now: () => clock.t,
    sleep: async (ms) => {
      clock.sleeps.push(ms);
      clock.t += ms;
    },
  };
  return clock;
}

/** An `attempt` that fails with `fail(n)` for the first `failures` calls. */
function attempts({ failures = Infinity, fail = (n) => networkError(`down #${n}`), took = 0, clock }) {
  const state = { n: 0 };
  state.run = async () => {
    state.n += 1;
    // A retry loop that ignores its budget must fail the test, not hang it.
    if (state.n > 1000) throw new Error('runaway retry loop');
    if (clock) clock.t += took;
    if (state.n <= failures) throw fail(state.n);
    return 'connected';
  };
  return state;
}

const policy = (clock, extra = {}) => ({
  shouldRetry: shouldRetryConnect,
  delayMs: CONNECT_RETRY_DELAY_MS,
  budgetMs: CONNECT_BUDGET_MS,
  sleep: clock.sleep,
  now: clock.now,
  ...extra,
});

test('the retry policy is every 3 s for a 60 s budget', () => {
  assert.equal(CONNECT_RETRY_DELAY_MS, 3000);
  assert.equal(CONNECT_BUDGET_MS, 60000);
});

test('retryWithin returns the first result without waiting when nothing fails', async () => {
  const clock = fakeClock();
  const a = attempts({ failures: 0 });
  const retried = [];
  const result = await retryWithin({
    attempt: a.run, ...policy(clock), onRetry: (e) => retried.push(e.message),
  });
  assert.equal(result, 'connected');
  assert.equal(a.n, 1);
  assert.deepEqual(clock.sleeps, []);
  assert.deepEqual(retried, []);
});

test('retryWithin keeps retrying a network failure until one attempt succeeds', async () => {
  const clock = fakeClock();
  const a = attempts({ failures: 5 });
  const result = await retryWithin({ attempt: a.run, ...policy(clock) });
  assert.equal(result, 'connected');
  assert.equal(a.n, 6);
  assert.deepEqual(clock.sleeps, [3000, 3000, 3000, 3000, 3000]);
});

test('retryWithin tells the caller about each retry before it waits', async () => {
  // This is how the page can say "Waking the server" at the first failure
  // rather than after the first pause.
  const clock = fakeClock();
  const a = attempts({ failures: 2 });
  const order = [];
  const sleep = async (ms) => { order.push('sleep'); await clock.sleep(ms); };
  await retryWithin({
    attempt: a.run, ...policy(clock), sleep, onRetry: (e) => order.push(e.message),
  });
  assert.deepEqual(order, ['down #1', 'sleep', 'down #2', 'sleep']);
});

test('retryWithin stops at the budget and surfaces the last failure', async () => {
  const clock = fakeClock();
  const a = attempts({});
  await assert.rejects(retryWithin({ attempt: a.run, ...policy(clock) }), { message: 'down #20' });
  // Attempts at 0, 3, ..., 57 s. A 21st would start at 60 s, which is past it.
  assert.equal(a.n, 20);
  assert.equal(clock.sleeps.length, 19);
  assert.equal(clock.t, 57000);
});

test('retryWithin counts the time an attempt itself takes against the budget', async () => {
  // Each try hangs for 20 s (the connect timeout) before failing: 0-20, wait,
  // 23-43, wait, 46-66, and 66 + 3 is well past the budget.
  const clock = fakeClock();
  const a = attempts({ took: 20000, clock });
  await assert.rejects(retryWithin({ attempt: a.run, ...policy(clock) }), { message: 'down #3' });
  assert.equal(a.n, 3);
  assert.equal(clock.t, 66000);
});

test('retryWithin does not start a retry that would begin at or after the budget', async () => {
  const clock = fakeClock();
  const a = attempts({});
  await assert.rejects(
    retryWithin({ attempt: a.run, ...policy(clock, { budgetMs: 6000 }) }),
    { message: 'down #2' }
  );
  assert.equal(a.n, 2, 'attempts at 0 and 3 s run; the one that would start at 6 s does not');
});

test('retryWithin never retries a failure that is not a network failure', async () => {
  const clock = fakeClock();
  const a = attempts({ fail: () => new Error('Deepgram rejected that API key.') });
  await assert.rejects(
    retryWithin({ attempt: a.run, ...policy(clock) }),
    { message: 'Deepgram rejected that API key.' }
  );
  assert.equal(a.n, 1);
  assert.deepEqual(clock.sleeps, []);

  // Also not after a network failure was already retried once.
  const mixed = attempts({ fail: (n) => (n === 1 ? networkError('down') : new Error('bad key')) });
  await assert.rejects(retryWithin({ attempt: mixed.run, ...policy(clock) }), { message: 'bad key' });
  assert.equal(mixed.n, 2);
});

test('retryWithin does not retry when the caller cancelled before the failure landed', async () => {
  const clock = fakeClock();
  const a = attempts({});
  await assert.rejects(
    retryWithin({ attempt: a.run, ...policy(clock, { cancelled: () => true }) }),
    { message: 'down #1' }
  );
  assert.equal(a.n, 1);
  assert.deepEqual(clock.sleeps, []);
});

test('retryWithin does not attempt again when cancelled during the delay', async () => {
  const clock = fakeClock();
  const a = attempts({});
  let cancelled = false;
  const sleep = async (ms) => { await clock.sleep(ms); cancelled = true; };
  await assert.rejects(
    retryWithin({ attempt: a.run, ...policy(clock, { sleep, cancelled: () => cancelled }) }),
    { message: 'down #1' }
  );
  assert.equal(a.n, 1, 'a Cancel pressed while waiting must not open another socket');
});

test('retryWithin stops between later attempts too', async () => {
  const clock = fakeClock();
  const a = attempts({});
  let cancelled = false;
  const sleep = async (ms) => {
    await clock.sleep(ms);
    if (clock.sleeps.length === 3) cancelled = true;
  };
  await assert.rejects(
    retryWithin({ attempt: a.run, ...policy(clock, { sleep, cancelled: () => cancelled }) }),
    { message: 'down #3' }
  );
  assert.equal(a.n, 3);
});
