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

/** Reject with `message` if `promise` has not settled within `ms`. */
export function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  // Clear on every outcome, or a settled call leaves a timer armed for later.
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
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

/**
 * Copy only the named, non-empty string entries. Whatever else is in
 * localStorage (an old build's fields, something a script put there) must not
 * ride along in the handshake.
 */
export function pickKeys(keys, fields) {
  const picked = {};
  if (!keys || typeof keys !== 'object') return picked;
  for (const name of fields) {
    const value = Object.hasOwn(keys, name) ? keys[name] : undefined;
    if (typeof value === 'string' && value) picked[name] = value;
  }
  return picked;
}
