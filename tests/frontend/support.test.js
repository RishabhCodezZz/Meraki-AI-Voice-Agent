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
  withTimeout,
  makeSessionId,
  isValidSessionId,
  pickKeys,
  networkError,
  shouldRetryConnect,
  retryOnce,
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

// --- retryOnce ---------------------------------------------------------------

/** A `run` that fails with the given errors in order, then succeeds. */
function flaky(...errors) {
  const calls = { n: 0 };
  const run = async () => {
    calls.n += 1;
    if (errors.length) throw errors.shift();
    return 'connected';
  };
  return { run, calls };
}

test('retryOnce returns the first result without waiting when nothing fails', async () => {
  const { run, calls } = flaky();
  const waits = [];
  const result = await retryOnce(run, {
    shouldRetry: () => true, delayMs: 2000, wait: (ms) => waits.push(ms),
  });
  assert.equal(result, 'connected');
  assert.equal(calls.n, 1);
  assert.deepEqual(waits, []);
});

test('retryOnce waits the delay and tries once more after a retryable failure', async () => {
  const { run, calls } = flaky(networkError('down'));
  const waits = [];
  const retried = [];
  const result = await retryOnce(run, {
    shouldRetry: shouldRetryConnect,
    delayMs: 2000,
    wait: async (ms) => { waits.push(ms); },
    onRetry: (error) => retried.push(error.message),
  });
  assert.equal(result, 'connected');
  assert.equal(calls.n, 2);
  assert.deepEqual(waits, [2000]);
  assert.deepEqual(retried, ['down']);
});

test('retryOnce gives up after one retry and surfaces the second failure', async () => {
  const { run, calls } = flaky(networkError('first'), networkError('second'));
  await assert.rejects(
    retryOnce(run, { shouldRetry: shouldRetryConnect, delayMs: 0, wait: async () => {} }),
    { message: 'second' }
  );
  assert.equal(calls.n, 2, 'never a third attempt');
});

test('retryOnce does not retry a failure the policy rejects', async () => {
  const { run, calls } = flaky(new Error('bad key'));
  await assert.rejects(
    retryOnce(run, { shouldRetry: shouldRetryConnect, delayMs: 0, wait: async () => {} }),
    { message: 'bad key' }
  );
  assert.equal(calls.n, 1);
});

test('retryOnce does not retry, or keep waiting, once the caller has cancelled', async () => {
  // Cancelled before the failure is even looked at.
  const first = flaky(networkError('down'));
  await assert.rejects(
    retryOnce(first.run, {
      shouldRetry: shouldRetryConnect, delayMs: 0, wait: async () => {}, cancelled: () => true,
    }),
    { message: 'down' }
  );
  assert.equal(first.calls.n, 1);

  // Cancelled while the delay was running: the retry must not start.
  let cancelled = false;
  const second = flaky(networkError('down'));
  await assert.rejects(
    retryOnce(second.run, {
      shouldRetry: shouldRetryConnect,
      delayMs: 2000,
      wait: async () => { cancelled = true; },
      cancelled: () => cancelled,
    }),
    { message: 'down' }
  );
  assert.equal(second.calls.n, 1, 'a stop pressed during the delay ends the start');
});
