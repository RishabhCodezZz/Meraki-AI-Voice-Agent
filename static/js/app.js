import { MicCapture } from './audio-capture.js';
import { SpeechPlayer } from './audio-player.js';
import { PlaybackGate } from './playback-gate.js';
import {
  CONNECT_BUDGET_MS,
  CONNECT_RETRY_DELAY_MS,
  describeStartError,
  detectSupport,
  endsTurn,
  isMicBlocked,
  isValidSessionId,
  makeSessionId,
  MIC_BLOCKED_ADVICE,
  networkError,
  pickKeys,
  retryWithin,
  shouldRetryConnect,
  withTimeout,
} from './support.js';
import { Visualizer } from './visualizer.js';

const KEY_FIELDS = ['deepgram', 'ollama', 'murf'];
const SERVICE_NAMES = { deepgram: 'Deepgram', ollama: 'Ollama', murf: 'Murf' };
const STORAGE_KEYS = 'meraki.keys';
// The server tells us whether it has keys of its own. If it does, visitors can
// just talk; if not, they must bring their own.
const KEYS_REQUIRED = document.body.dataset.keysRequired !== 'false';

// A connect that is still waiting after this long is almost certainly a sleeping
// free-tier server booting, so say so instead of sitting on "Connecting".
const COLD_START_MS = 5000;
const CONNECT_TIMEOUT_MS = 20000;

const el = (id) => document.getElementById(id);

const ui = {
  status: el('status'),
  statusDot: el('status-dot'),
  meter: el('meter'),
  micBtn: el('mic-btn'),
  micLabel: el('mic-label'),
  hint: el('hint'),
  stopSpeaking: el('stop-speaking'),
  banner: el('banner'),
  bannerText: el('banner-text'),
  bannerRetry: el('banner-retry'),
  live: el('live'),
  liveUser: el('live-user'),
  liveReply: el('live-reply'),
  transcript: el('transcript'),
  empty: el('empty'),
  clearBtn: el('clear-btn'),
  settings: el('settings'),
  settingsForm: el('settings-form'),
  settingsBtn: el('settings-btn'),
  closeSettings: el('close-settings'),
  forgetKeys: el('forget-keys'),
  toggleKeys: el('toggle-keys'),
  toasts: el('toasts'),
};
// The status chip: a button only while it says "Needs keys".
ui.chip = ui.statusDot.parentElement;

const visualizer = new Visualizer(ui.meter);
visualizer.start();

let socket = null;
let mic = null;
let player = null;
let gate = null;
let recording = false;
let replyBuffer = '';
// Bumped by every start and every stop. A start that wakes from an await and
// finds it changed was cancelled (or superseded) while it waited, and must put
// down whatever it made without touching the state a newer session now owns.
let generation = 0;
// Set at boot. When the browser cannot do the job nothing should start, and the
// chip must not claim "Ready" on its way back to rest.
let supported = true;
let supportReason = '';
// Set by "Stop speaking". The server's turn carries on after the button is
// pressed, so the rest of its audio and caption would arrive and play right
// back; this drops them until the next turn starts.
let muteReply = false;
// Set by Clear. Like muteReply, but also stops the reply text being filed in a
// transcript the visitor has just emptied. Both reset when the next turn starts.
let discardReply = false;
// The connect attempt a start is currently making, hoisted so a Cancel (which
// runs stopRecording, not the start's own finally) can close its socket at once.
let pendingAttempt = null;
// True from the moment a start begins until it has succeeded, failed or been
// cancelled: the whole connect, retries and microphone prompt included. Cancel
// is decided on this, not on what the chip says, because other handlers (Clear,
// saving keys) call restIdle() in the middle of a start.
let starting = false;

// --- session -----------------------------------------------------------------

function sessionId() {
  const url = new URL(window.location.href);
  let id = url.searchParams.get('s');
  // Not just "missing": the server mints its own id for one it will not keep, and
  // a link with such an id would then load history that never resumes.
  if (!isValidSessionId(id)) {
    id = makeSessionId();
    url.searchParams.set('s', id);
    history.replaceState({}, '', url);
  }
  return id;
}

// --- keys --------------------------------------------------------------------
// Kept in this browser only. They are sent once, in the opening frame of your
// own WebSocket, and the server holds them on that connection alone.

function loadKeys() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEYS)) || {};
  } catch {
    return {};
  }
}

function saveKeys(keys) {
  try {
    if (Object.keys(keys).length) {
      localStorage.setItem(STORAGE_KEYS, JSON.stringify(keys));
    } else {
      localStorage.removeItem(STORAGE_KEYS);
    }
    return true;
  } catch {
    toast('This browser will not let the page store anything.', 'error');
    return false;
  }
}

/** Which required keys we have neither locally nor on the server. */
function missingKeys() {
  if (!KEYS_REQUIRED) return [];
  const keys = loadKeys();
  return KEY_FIELDS.filter((name) => !keys[name]);
}

// --- chrome ------------------------------------------------------------------

let uiState = 'idle';
let chipOpensKeys = false;

const START_HINT = 'Click to start a conversation. Space also works.';
const HINTS = {
  idle: START_HINT,
  blocked: START_HINT,
  connecting: 'Setting things up…',
  listening: 'Just talk. Interrupt any time.',
  thinking: 'Just talk. Interrupt any time.',
  speaking: 'Talk over it to interrupt.',
};

function setState(state, label, { opensKeys = false } = {}) {
  uiState = state;
  visualizer.setState(state);
  ui.status.textContent = label;
  ui.statusDot.dataset.state = state;
  ui.chip.dataset.state = state;
  document.body.dataset.state = state;
  ui.hint.textContent = HINTS[state] || ui.hint.textContent;

  // The stop button disappears with the state; a keyboard user who pressed it
  // would otherwise land on <body>, where Space ends the whole session.
  const leaving = state !== 'speaking' && document.activeElement === ui.stopSpeaking;
  ui.stopSpeaking.hidden = state !== 'speaking';
  if (leaving) ui.micBtn.focus();

  // "Needs keys" is something to act on, so it is reachable and operable; every
  // other label is just a status and must not be announced as a control.
  chipOpensKeys = opensKeys;
  if (opensKeys) {
    ui.chip.tabIndex = 0;
    ui.chip.setAttribute('role', 'button');
    // The visible text is only a status; this says what pressing it does.
    ui.chip.setAttribute('aria-label', 'Needs keys: open key settings');
  } else {
    // Saving keys from the dialog hands focus back to the chip; taking away its
    // tabindex while it holds focus would drop focus onto <body>.
    if (document.activeElement === ui.chip) ui.micBtn.focus();
    ui.chip.removeAttribute('tabindex');
    ui.chip.removeAttribute('role');
    ui.chip.removeAttribute('aria-label');
  }
}

/** What the chip should read when no conversation is running. */
function restIdle() {
  // A start in progress owns the chip, hint and button; it settles them itself
  // when it ends, after clearing `starting`.
  if (starting) return;
  if (!supported) {
    setState('blocked', 'Unsupported');
    ui.hint.textContent = supportReason;
  } else if (missingKeys().length) {
    setState('blocked', 'Needs keys', { opensKeys: true });
  } else {
    setState('idle', 'Ready');
  }
}

function toast(message, kind = 'info') {
  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  // The container is polite, which suits a confirmation; a failure should cut in.
  node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  node.textContent = message;
  ui.toasts.appendChild(node);
  requestAnimationFrame(() => node.classList.add('is-in'));
  setTimeout(() => {
    node.classList.remove('is-in');
    setTimeout(() => node.remove(), 250);
  }, 4200);
}

// --- banner ------------------------------------------------------------------
// A failure that outlives its toast. One button serves two jobs, so what it does
// is remembered here: 'retry' starts again, 'keys' opens the key dialog.

let bannerAction = null;

function showBanner(message, { retry = false, openKeys = false } = {}) {
  ui.bannerText.textContent = message;
  bannerAction = retry ? 'retry' : openKeys ? 'keys' : null;
  ui.bannerRetry.hidden = bannerAction === null;
  ui.bannerRetry.textContent = retry ? 'Retry' : 'Open Keys';
  ui.banner.hidden = false;
}

function hideBanner() {
  // Pressed Retry: the button is about to vanish, so keep focus on something real.
  if (ui.banner.contains(document.activeElement)) ui.micBtn.focus();
  ui.banner.hidden = true;
  ui.bannerText.textContent = '';
  bannerAction = null;
}

/**
 * A failure that ends the conversation. The banner is the lasting copy, so it
 * is not also a toast (a screen reader would hear it twice). A rejected key
 * gets "Open Keys": retrying with the same key can only fail the same way.
 */
function showFailure(message, code) {
  showBanner(message, code === 'keys' ? { openKeys: true } : { retry: true });
}

function addTurn(role, content) {
  const log = ui.transcript;
  // Measure before appending: if you have scrolled up to read something, a new
  // turn should not yank you back down.
  const pinned =
    log.scrollHeight - log.scrollTop - log.clientHeight < 60;

  ui.empty.hidden = true;
  const row = document.createElement('div');
  row.className = `turn turn--${role}`;
  const who = document.createElement('span');
  who.className = 'turn__who';
  who.textContent = role === 'user' ? 'You' : 'Meraki';
  const body = document.createElement('p');
  body.className = 'turn__body';
  body.textContent = content;
  row.append(who, body);
  log.append(row);
  if (pinned) log.scrollTop = log.scrollHeight;
}

function showLive({ user, reply }) {
  if (user !== undefined) ui.liveUser.textContent = user;
  if (reply !== undefined) ui.liveReply.textContent = reply;
}

function clearLive() {
  ui.liveUser.textContent = '';
  ui.liveReply.textContent = '';
}

// --- history -----------------------------------------------------------------

const EMPTY_TEXT = ui.empty.textContent;
let historyLoad = 0;

async function loadHistory() {
  // Without this, "Nothing yet" sits there as if it were the answer while the
  // request is still out.
  const mine = ++historyLoad;
  ui.empty.textContent = 'Loading conversation…';
  try {
    const response = await fetch(`/api/history/${encodeURIComponent(sessionId())}`);
    if (!response.ok) return;
    const { history } = await response.json();
    const log = ui.transcript;
    // Rebuilding the list sends it to the bottom (every addTurn pins an empty
    // log); someone who had scrolled up to read should stay where they were.
    const scrolledUp = log.scrollHeight - log.scrollTop - log.clientHeight >= 60;
    const top = log.scrollTop;
    log.replaceChildren();
    history.forEach((turn) => addTurn(turn.role, turn.content));
    ui.empty.hidden = history.length > 0;
    if (scrolledUp) log.scrollTop = top;
  } catch {
    /* history is a nicety; never block startup on it */
  } finally {
    // A newer load owns the text now; only the last one standing restores it.
    if (mine === historyLoad) ui.empty.textContent = EMPTY_TEXT;
  }
}

async function clearHistory() {
  try {
    const response = await fetch(`/api/history/${encodeURIComponent(sessionId())}`, {
      method: 'DELETE',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch {
    // Saying "cleared" over a transcript that is still on the server would be a lie.
    toast('Could not clear the conversation.', 'error');
    return;
  }
  // Whatever is still being spoken belongs to the conversation just wiped. The
  // server's turn carries on (nothing tells it otherwise), so what it still
  // sends is dropped here, as for Stop speaking, and its reply text is not
  // filed into the empty transcript either. The gate still settles on the
  // turn's own speech_done.
  muteReply = true;
  discardReply = true;
  player?.flush();
  gate?.cancel();
  replyBuffer = '';
  if (recording) setState('listening', 'Listening');
  else restIdle();
  ui.transcript.replaceChildren();
  ui.empty.hidden = false;
  clearLive();
  toast('Conversation cleared.');
}

// --- socket ------------------------------------------------------------------

function socketUrl() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.host}/ws`;
}

function closeQuietly(ws) {
  try {
    ws.close();
  } catch {
    /* already closing */
  }
}

/**
 * Open the socket and resolve once the server says `ready`. `attempt.ws` is the
 * socket as soon as it exists, so a caller that gives up (timeout, cancel) can
 * close it even though this promise has not settled.
 */
function connect(attempt) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(socketUrl());
    attempt.ws = ws;
    ws.binaryType = 'arraybuffer';

    // The promise must settle on every path. A fatal error can arrive before
    // 'ready' (a bad Deepgram key, say), and if that left the promise pending
    // startRecording would await forever with the button stuck disabled.
    let settled = false;
    const succeed = () => {
      if (settled) return;
      settled = true;
      resolve(ws);
    };
    const failed = (error) => {
      if (settled) return;
      settled = true;
      closeQuietly(ws);
      reject(error);
    };

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: 'config',
          session_id: sessionId(),
          keys: pickKeys(loadKeys(), KEY_FIELDS),
        })
      );
    };

    ws.onmessage = (event) => {
      // A socket that has been replaced or stopped can still deliver frames
      // already in flight; they belong to a conversation that is over.
      if (settled && ws !== socket) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === 'ready') {
        ws.onerror = null;
        succeed();
        return;
      }
      if (message.type === 'error' && !settled) {
        // Surfaced by startRecording's catch; don't double-toast it here.
        // Not a network failure, so not retried: it will say the same again.
        failed(Object.assign(new Error(message.message), { code: message.code }));
        return;
      }
      handleMessage(message);
    };

    ws.onerror = () => failed(networkError('Could not reach the server.'));
    ws.onclose = () => {
      failed(networkError('The server closed the connection.'));
      // Not the live socket: stop() or a newer start already moved on, and
      // clearing `socket` here would knock out the session that replaced it.
      if (ws !== socket) return;
      // Clearing it is also what lets startRecording notice a close that came
      // during the microphone prompt.
      socket = null;
      if (recording) {
        stopRecording({ silent: true });
        showFailure('Connection lost.');
      }
    };
  });
}

function handleMessage(message) {
  switch (message.type) {
    case 'partial':
      showLive({ user: message.text });
      // Two words, like the server's own threshold: one is too twitchy against
      // residual echo. Cutting here rather than waiting for the server's
      // `interrupted` saves the round trip you would otherwise hear as the old
      // reply talking over you.
      if (player.playing && message.text.trim().split(/\s+/).length >= 2) {
        player.flush();
        gate.cancel();
        replyBuffer = '';
        showLive({ reply: '' });
        setState('listening', 'Listening');
      }
      break;

    case 'final':
      addTurn('user', message.text);
      showLive({ user: '', reply: '' });
      replyBuffer = '';
      // The server has already dropped echo before sending this, so a final is
      // really the user. Without the cut, an answer to it queues behind the
      // old reply's audio that is still waiting to play.
      player.flush();
      gate.cancel();
      if (recording) setState('listening', 'Listening');
      break;

    case 'thinking':
      player.flush();
      gate.turnStarted();
      muteReply = false;
      discardReply = false;
      setState('thinking', 'Thinking');
      break;

    case 'reply_chunk':
      if (muteReply) break;
      replyBuffer += message.text;
      showLive({ reply: replyBuffer });
      break;

    case 'reply_done':
      if (!discardReply) addTurn('assistant', message.text);
      clearLive();
      replyBuffer = '';
      break;

    case 'audio':
      if (muteReply) break;
      setState('speaking', 'Speaking');
      player.enqueue(message.data).catch(() => {
        toast('Could not play that audio chunk.', 'error');
      });
      break;

    case 'speech_done':
      // Sent right behind the last audio frame, usually before it has played;
      // the gate waits for the player to run dry before settling.
      gate.turnEnded();
      break;

    case 'interrupted':
      gate.cancel();
      player.flush();
      replyBuffer = '';
      showLive({ reply: '' });
      // Not recording means the user has already stopped; claiming to listen
      // would be wrong.
      if (recording) setState('listening', 'Listening');
      else restIdle();
      break;

    case 'error':
      if (!message.fatal) toast(message.message, 'error');
      if (message.fatal) {
        showFailure(message.message, message.code);
        gate.cancel();
        stopRecording({ silent: true });
      } else if (!endsTurn(message.code)) {
        // A transcription hiccup (code `stt`) says nothing about the reply being
        // spoken. Ending the turn here settled the gate early; the real
        // speech_done was then ignored and "Speaking" stuck. Toast only.
      } else if (gate.turnOpen) {
        // The turn is over without (more) audio; settle once what is queued
        // has played.
        gate.turnEnded();
      } else if (recording) {
        setState('listening', 'Listening');
      } else {
        restIdle();
      }
      break;
  }
}

// --- recording ---------------------------------------------------------------

async function startRecording() {
  if (!supported) return;
  hideBanner();
  if (missingKeys().length) {
    // The dialog opens over this; the banner is what is left when it is closed
    // without saving, and "Open Keys" brings it back.
    showBanner('Add your API keys to get started.', { openKeys: true });
    openSettings();
    return;
  }

  const mine = ++generation;
  starting = true;
  const cancelled = () => mine !== generation;
  const attempt = { ws: null };
  pendingAttempt = attempt;
  let capture = null;
  muteReply = false;

  // Connecting can take a minute on a cold start, so the button stays live and
  // turns into Cancel (toggleRecording) rather than being a dead end.
  setState('connecting', 'Connecting');
  ui.micBtn.dataset.active = 'true';
  ui.micLabel.textContent = 'Cancel';

  const showWaking = () => {
    if (cancelled() || !starting) return;
    setState('connecting', 'Waking the server…');
    ui.hint.textContent = 'A sleeping free server can take up to a minute.';
  };
  // Two ways to know: a connect that hangs (this timer), and one that is
  // refused at once, which is what a booting Render container does (onRetry).
  // Armed for the connect only; the microphone prompt that follows can take as
  // long as the visitor likes, and that is not the server waking up.
  const slowTimer = setTimeout(showWaking, COLD_START_MS);

  try {
    player = player || new SpeechPlayer({
      onLevel: (bins) => visualizer.setSpectrum(bins),
      onIdle: () => gate.playerIdle(),
    });
    gate = gate || new PlaybackGate({
      isPlaying: () => player.playing,
      onSettled: () => {
        if (recording) setState('listening', 'Listening');
        else restIdle();
      },
    });
    capture = new MicCapture({
      onFrame: (buffer) => {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(buffer);
      },
      onLevel: (bins) => {
        if (uiState === 'listening') visualizer.setSpectrum(bins);
      },
      onEnded: () => {
        if (mic !== capture) return;
        toast('The microphone was disconnected.', 'error');
        stopRecording({ silent: true });
      },
    });
    mic = capture;

    // Both audio contexts are created here, before any await, so they are made
    // inside the click gesture. Safari and iOS refuse to start one otherwise.
    await Promise.all([player.ensureContext(), capture.prepare()]);
    if (cancelled()) return;

    const ws = await retryWithin({
      attempt: () => {
        // The previous try's socket may still be open (a timeout does not close it).
        if (attempt.ws) closeQuietly(attempt.ws);
        return withTimeout(
          connect(attempt),
          CONNECT_TIMEOUT_MS,
          'The server took too long to answer.',
          networkError
        );
      },
      shouldRetry: shouldRetryConnect,
      delayMs: CONNECT_RETRY_DELAY_MS,
      budgetMs: CONNECT_BUDGET_MS,
      cancelled,
      onRetry: showWaking,
    });
    clearTimeout(slowTimer);
    if (cancelled()) return;
    // Connected. If the slow notice went up, take it down for the mic prompt.
    setState('connecting', 'Connecting');
    socket = ws;

    await capture.start();
    if (cancelled()) return;
    // The server can hang up while the browser is still asking about the mic.
    if (!socket || socket !== ws || socket.readyState !== WebSocket.OPEN) {
      throw new Error('The connection dropped while starting.');
    }

    recording = true;
    starting = false; // before the state below, or restIdle() would ignore it
    ui.micBtn.dataset.active = 'true';
    ui.micLabel.textContent = 'Stop';
    setState('listening', 'Listening');
  } catch (error) {
    // A cancelled start says nothing: the visitor asked for it to stop.
    if (!cancelled()) {
      if (isMicBlocked(error)) {
        // Retry would be refused again at once; say what to change instead.
        showBanner(`${describeStartError(error)} ${MIC_BLOCKED_ADVICE}`);
      } else {
        showFailure(describeStartError(error), error?.code);
      }
      await stopRecording({ silent: true });
    }
  } finally {
    clearTimeout(slowTimer);
    // A pending socket that never became `socket` (timeout, early close, a
    // cancelled start) is ours to close; the live one is stopRecording's. The
    // same goes for a mic that a stop never got to see.
    if (attempt.ws && attempt.ws !== socket) closeQuietly(attempt.ws);
    // Only this start's own end: a newer start may already own the flag.
    if (!cancelled()) starting = false;
    if (cancelled() && capture && capture !== mic) capture.stop();
    if (pendingAttempt === attempt) pendingAttempt = null;
  }
}

async function stopRecording({ silent = false } = {}) {
  generation++; // cancels a start that is still waiting on something
  starting = false; // ahead of restIdle() below, which would otherwise be skipped
  recording = false;
  ui.micBtn.dataset.active = 'false';
  ui.micBtn.disabled = !supported;
  ui.micLabel.textContent = 'Start talking';

  // Take ownership and clear the module state before awaiting anything: a
  // start pressed while mic.stop() is still closing the context must find a
  // clean slate, not have its new mic and socket nulled underneath it.
  const stopping = mic;
  const closing = socket;
  mic = null;
  socket = null;
  // A Cancel during the connect: the start is still waiting on this socket (or
  // on the pause before its next try), and should not wait for the timeout.
  if (pendingAttempt?.ws && pendingAttempt.ws !== closing) closeQuietly(pendingAttempt.ws);
  pendingAttempt = null;

  if (closing && closing.readyState === WebSocket.OPEN) {
    closing.send('stop');
    closing.close();
  }
  if (gate) gate.cancel();
  if (player) player.flush();
  replyBuffer = '';
  muteReply = false;
  clearLive();
  restIdle();
  if (!silent) loadHistory();
  if (stopping) await stopping.stop();
}

function toggleRecording() {
  if (recording) stopRecording();
  // Still connecting: the button says Cancel. Bumping the generation (inside
  // stopRecording) is what makes the in-flight start and its retries give up.
  else if (starting) stopRecording({ silent: true });
  else startRecording();
}

/**
 * Cut Meraki off by hand. Nothing is sent: the server has no such message, and
 * the next thing the visitor says replaces its turn anyway. What it keeps
 * sending in the meantime is dropped (muteReply) rather than played.
 */
function stopSpeaking() {
  if (uiState !== 'speaking') return;
  muteReply = true;
  player.flush();
  gate.cancel();
  replyBuffer = '';
  showLive({ reply: '' });
  if (recording) setState('listening', 'Listening');
  else restIdle();
}

// --- settings ----------------------------------------------------------------

const keyInput = (name) => el(`key-${name}`);

function showKeyError(name, message) {
  const error = el(`key-error-${name}`);
  error.textContent = message;
  error.hidden = false;
  keyInput(name).setAttribute('aria-invalid', 'true');
}

function clearKeyError(name) {
  const error = el(`key-error-${name}`);
  error.textContent = '';
  error.hidden = true;
  keyInput(name).removeAttribute('aria-invalid');
}

/** The three-step row: a tick for every field that has something in it. */
function updateChecklist() {
  KEY_FIELDS.forEach((name) => {
    const done = keyInput(name).value.trim() !== '';
    const step = el(`key-step-${name}`);
    step.dataset.done = String(done);
    step.querySelector('.steps__mark').textContent = done ? '\u2713' : '\u2022';
    // The mark is decoration to a screen reader; this is what it hears instead.
    step.querySelector('.sr-only').textContent = done ? ' added' : ' not added';
  });
}

function setKeysVisible(visible) {
  KEY_FIELDS.forEach((name) => {
    keyInput(name).type = visible ? 'text' : 'password';
  });
  ui.toggleKeys.textContent = visible ? 'Hide' : 'Show';
  // The visible word alone is ambiguous out of context, and the label changes
  // with the state, so this is not also a pressed/unpressed toggle.
  ui.toggleKeys.setAttribute('aria-label', visible ? 'Hide keys' : 'Show keys');
}

function openSettings() {
  if (ui.settings.open) return;
  const keys = loadKeys();
  KEY_FIELDS.forEach((name) => {
    keyInput(name).value = keys[name] || '';
    clearKeyError(name);
  });
  setKeysVisible(false);
  updateChecklist();
  // A modal dialog sits above everything else on the page, toasts included.
  // While it is open they live inside it, or "Keys removed" would play out
  // behind the backdrop where nobody can see it.
  ui.settings.append(ui.toasts);
  ui.settings.showModal();
  // showModal() focuses the first control it finds (Close); the visitor wants
  // the first key they still have to paste.
  const target = KEY_FIELDS.map(keyInput).find((input) => !input.value) || keyInput(KEY_FIELDS[0]);
  target.focus();
}

function submitSettings(event) {
  event.preventDefault();
  const keys = {};
  KEY_FIELDS.forEach((name) => {
    const value = keyInput(name).value.trim();
    if (value) keys[name] = value;
    clearKeyError(name);
  });

  const missing = KEY_FIELDS.filter((name) => !keys[name]);
  if (KEYS_REQUIRED && missing.length) {
    // Inline, under the field: a toast would render behind the dialog.
    missing.forEach((name) => showKeyError(name, `Paste your ${SERVICE_NAMES[name]} key.`));
    keyInput(missing[0]).focus();
    return;
  }

  if (!saveKeys(keys)) return;
  ui.settings.close();
  // `close` fires a task later; the toast below must not land in a hidden dialog.
  document.body.append(ui.toasts);
  if (!recording) restIdle();
  // The keys it was asking for are in; the banner would be stale.
  if (bannerAction === 'keys' && !missingKeys().length) hideBanner();
  toast(
    Object.keys(keys).length
      ? 'Saved on this device.'
      : 'Cleared. Falling back to the server keys.'
  );
  if (recording) toast('New keys apply next time you start.');
}

function forgetKeys() {
  KEY_FIELDS.forEach((name) => {
    keyInput(name).value = '';
    clearKeyError(name);
  });
  updateChecklist();
  saveKeys({});
  if (!recording) restIdle();
  toast('Keys removed from this browser.');
}

// --- boot --------------------------------------------------------------------

ui.micBtn.addEventListener('click', toggleRecording);
ui.clearBtn.addEventListener('click', clearHistory);
ui.settingsBtn.addEventListener('click', openSettings);
ui.closeSettings.addEventListener('click', () => ui.settings.close());
ui.settingsForm.addEventListener('submit', submitSettings);
ui.forgetKeys.addEventListener('click', forgetKeys);
ui.toggleKeys.addEventListener('click', () => {
  setKeysVisible(keyInput(KEY_FIELDS[0]).type === 'password');
});
ui.stopSpeaking.addEventListener('click', stopSpeaking);
ui.bannerRetry.addEventListener('click', () => {
  if (bannerAction === 'keys') {
    openSettings();
  } else if (bannerAction === 'retry' && !recording) {
    startRecording(); // hides the banner itself
  }
});

KEY_FIELDS.forEach((name) => {
  keyInput(name).addEventListener('input', () => {
    updateChecklist();
    clearKeyError(name);
  });
});

// A click on the backdrop lands on the dialog element itself (the form fills
// the rest). It only counts if the press started there too: dragging to select
// a key and letting go outside the box also ends in a click on the dialog.
let pressedBackdrop = false;
ui.settings.addEventListener('pointerdown', (event) => {
  pressedBackdrop = event.target === ui.settings;
});
ui.settings.addEventListener('click', (event) => {
  if (event.target === ui.settings && pressedBackdrop) ui.settings.close();
  pressedBackdrop = false;
});
ui.settings.addEventListener('close', () => {
  // `close` is dispatched a task later; if the dialog has been opened again by
  // then, the toasts belong inside it.
  if (ui.settings.open) return;
  document.body.append(ui.toasts);
  // Do not leave pasted keys readable behind a dialog that looks closed.
  setKeysVisible(false);
});

ui.chip.addEventListener('click', () => {
  if (chipOpensKeys) openSettings();
});
ui.chip.addEventListener('keydown', (event) => {
  if (!chipOpensKeys || (event.key !== 'Enter' && event.key !== ' ')) return;
  event.preventDefault();
  openSettings();
});

document.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' || event.target !== document.body) return;
  event.preventDefault();
  // Holding the key repeats keydown; each repeat would toggle the session.
  if (event.repeat) return;
  // A click on a disabled button (the unsupported case) is swallowed by the
  // browser; this path is not. A press during "Connecting" is a Cancel, which
  // toggleRecording handles, so it cannot start a second session.
  if (ui.micBtn.disabled) return;
  toggleRecording();
});

window.addEventListener('beforeunload', () => {
  if (socket) socket.close();
});

const support = detectSupport();
if (!support.ok) {
  supported = false;
  supportReason = support.reason;
  ui.micBtn.disabled = true;
}

sessionId();
loadHistory();
restIdle();
if (supported && missingKeys().length) openSettings();
