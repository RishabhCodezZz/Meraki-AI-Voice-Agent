/**
 * Small pure helpers for app.js: what the browser can do, how to phrase what
 * went wrong, and a timeout. Kept free of the DOM so they can be unit tested.
 */

/**
 * Can this page run a conversation at all? Answers with the first thing that is
 * missing, in plain language, so the page can say why instead of failing later
 * with a stack trace-shaped message.
 */
export function detectSupport(env = globalThis) {
  if (!env.isSecureContext) {
    return {
      ok: false,
      reason: 'Microphone access needs a secure connection (https or localhost).',
    };
  }
  if (!env.navigator?.mediaDevices?.getUserMedia) {
    return { ok: false, reason: 'This browser cannot capture audio.' };
  }
  if (
    !(env.AudioContext || env.webkitAudioContext) ||
    !env.AudioWorkletNode ||
    !env.WebSocket
  ) {
    return {
      ok: false,
      reason: 'This browser does not support the audio features Meraki needs.',
    };
  }
  return { ok: true };
}

/** Turn whatever start-up threw into something a visitor can act on. */
export function describeStartError(error) {
  switch (error?.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access was blocked.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone was found.';
    case 'NotReadableError':
      return 'The microphone is busy or unavailable.';
    default:
      return error?.message || 'Could not start.';
  }
}

/**
 * Reject with `message` if `promise` has not settled within `ms`. `makeError`
 * builds the rejection, for callers that need to tell a timeout apart.
 */
export function withTimeout(promise, ms, message, makeError = (text) => new Error(text)) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(makeError(message)), ms);
  });
  // Clear on every outcome, or a settled call leaves a timer armed for later.
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * A failure of the transport itself: nothing answered, or the line dropped
 * before the server said ready. Marked rather than recognised by its wording,
 * so a server `error` frame can never be mistaken for one.
 */
export function networkError(message) {
  const error = new Error(message);
  error.network = true;
  return error;
}

/**
 * Should a failed connect be tried again? Only a network failure should: a
 * sleeping free server answers the second knock, but a rejected key or a bad
 * handshake gets the same answer every time, and a missing microphone is not a
 * connection problem at all.
 */
export function shouldRetryConnect(error) {
  return error instanceof Error && error.network === true;
}

// Render's free tier answers a request for a sleeping container with an
// immediate 404 and keeps doing so for 30-60 s while it boots, so a WebSocket
// upgrade fails fast, over and over. Retrying every few seconds for about a
// minute is what turns that into "Waking the server" and then a connection.
export const CONNECT_RETRY_DELAY_MS = 3000;
export const CONNECT_BUDGET_MS = 60000;

/**
 * Keep calling `attempt` while it fails in a way `shouldRetry` accepts, pausing
 * `delayMs` between tries, until `budgetMs` has passed since the first try
 * began. The time an attempt itself takes counts, and no retry is started that
 * would begin at or after the budget. `onRetry(error)` fires as soon as a retry
 * is decided, before the pause, so the caller can show it at the first failure.
 *
 * `cancelled` is checked once a failure lands and again after each pause, so a
 * Cancel pressed while waiting never opens another connection. Whatever stops
 * the loop (budget, policy, cancel) rejects with the failure that caused it.
 * `sleep` and `now` can be replaced so tests need not wait.
 */
export async function retryWithin({
  attempt,
  shouldRetry,
  delayMs,
  budgetMs,
  cancelled = () => false,
  onRetry = () => {},
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = () => Date.now(),
}) {
  const started = now();
  for (;;) {
    try {
      return await attempt();
    } catch (error) {
      if (cancelled() || !shouldRetry(error)) throw error;
      if (now() - started + delayMs >= budgetMs) throw error;
      onRetry(error);
      await sleep(delayMs);
      if (cancelled()) throw error;
    }
  }
}

const ID_ALPHABET =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-';

/**
 * A session id the server will accept: 16-32 characters of [A-Za-z0-9_-]. The
 * server keeps ids of 8-64 and silently mints its own for anything else, which
 * would orphan the `?s=` link; a bare Math.random().toString(36) can fall short.
 */
export function makeSessionId(env = globalThis) {
  const crypto = env.crypto;
  try {
    if (crypto?.randomUUID) return crypto.randomUUID().replace(/-/g, '');
    if (crypto?.getRandomValues) {
      // 64 symbols divide 256 evenly, so masking a byte is unbiased.
      const bytes = crypto.getRandomValues(new Uint8Array(24));
      return Array.from(bytes, (byte) => ID_ALPHABET[byte & 63]).join('');
    }
  } catch {
    /* fall through to the weak source below */
  }
  // No usable crypto: the clock keeps two ids apart, Math.random pads the rest.
  let id = Date.now().toString(36);
  while (id.length < 24) id += ID_ALPHABET[Math.floor(Math.random() * 64)];
  return id.slice(0, 32);
}

const SESSION_ID_SHAPE = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * Would the server keep this id? Mirrors its pattern. A link with anything else
 * in `?s=` (hand-edited, or from an older build) would otherwise load history
 * under one id while the server quietly talks under another.
 */
export function isValidSessionId(id) {
  return typeof id === 'string' && SESSION_ID_SHAPE.test(id);
}

/**
 * Copy only the named, non-empty string entries. Whatever else is in
 * localStorage (an old build's fields, something a script put there) must not
 * ride along in the handshake.
 */
export function pickKeys(keys, fields) {
  const picked = {};
  if (!keys || typeof keys !== 'object') return picked;
  for (const name of fields) {
    const value = Object.prototype.hasOwnProperty.call(keys, name) ? keys[name] : undefined;
    if (typeof value === 'string' && value) picked[name] = value;
  }
  return picked;
}
