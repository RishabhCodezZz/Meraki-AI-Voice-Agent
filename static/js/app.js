import { MicCapture } from './audio-capture.js';
import { SpeechPlayer } from './audio-player.js';
import { PlaybackGate } from './playback-gate.js';
import {
  describeStartError,
  detectSupport,
  makeSessionId,
  pickKeys,
  withTimeout,
} from './support.js';
import { Visualizer } from './visualizer.js';

const KEY_FIELDS = ['deepgram', 'ollama', 'murf'];
const STORAGE_KEYS = 'meraki.keys';
// The server tells us whether it has keys of its own. If it does, visitors can
// just talk; if not, they must bring their own.
const KEYS_REQUIRED = document.body.dataset.keysRequired !== 'false';

const el = (id) => document.getElementById(id);

const ui = {
  status: el('status'),
  statusDot: el('status-dot'),
  meter: el('meter'),
  micBtn: el('mic-btn'),
  micLabel: el('mic-label'),
  hint: el('hint'),
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
  toasts: el('toasts'),
};

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

// --- session -----------------------------------------------------------------

function sessionId() {
  const url = new URL(window.location.href);
  let id = url.searchParams.get('s');
  if (!id) {
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

function setState(state, label) {
  uiState = state;
  visualizer.setState(state);
  ui.status.textContent = label;
  ui.statusDot.dataset.state = state;
  ui.statusDot.parentElement.dataset.state = state;
  document.body.dataset.state = state;
}

/** What the chip should read when no conversation is running. */
function restIdle() {
  if (!supported) setState('blocked', 'Unsupported');
  else if (missingKeys().length) setState('blocked', 'Needs keys');
  else setState('idle', 'Ready');
}

function toast(message, kind = 'info') {
  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  ui.toasts.appendChild(node);
  requestAnimationFrame(() => node.classList.add('is-in'));
  setTimeout(() => {
    node.classList.remove('is-in');
    setTimeout(() => node.remove(), 250);
  }, 4200);
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

async function loadHistory() {
  try {
    const response = await fetch(`/api/history/${encodeURIComponent(sessionId())}`);
    if (!response.ok) return;
    const { history } = await response.json();
    ui.transcript.replaceChildren();
    history.forEach((turn) => addTurn(turn.role, turn.content));
    ui.empty.hidden = history.length > 0;
  } catch {
    /* history is a nicety; never block startup on it */
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
  // Whatever is still being spoken belongs to the conversation just wiped.
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
    const failed = (message) => {
      if (settled) return;
      settled = true;
      closeQuietly(ws);
      reject(new Error(message || 'Could not reach the server.'));
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
        failed(message.message);
        return;
      }
      handleMessage(message);
    };

    ws.onerror = () => failed();
    ws.onclose = () => {
      failed('The server closed the connection.');
      // Not the live socket: stop() or a newer start already moved on, and
      // clearing `socket` here would knock out the session that replaced it.
      if (ws !== socket) return;
      // Clearing it is also what lets startRecording notice a close that came
      // during the microphone prompt.
      socket = null;
      if (recording) {
        stopRecording({ silent: true });
        toast('Connection lost.', 'error');
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
      setState('thinking', 'Thinking');
      break;

    case 'reply_chunk':
      replyBuffer += message.text;
      showLive({ reply: replyBuffer });
      break;

    case 'reply_done':
      addTurn('assistant', message.text);
      clearLive();
      replyBuffer = '';
      break;

    case 'audio':
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
      toast(message.message, 'error');
      if (message.fatal) {
        gate.cancel();
        stopRecording({ silent: true });
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
  if (missingKeys().length) {
    toast('Add your API keys to get started.', 'error');
    openSettings();
    return;
  }

  const mine = ++generation;
  const cancelled = () => mine !== generation;
  const attempt = { ws: null };
  let capture = null;

  setState('connecting', 'Connecting');
  ui.micBtn.disabled = true;

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

    const ws = await withTimeout(
      connect(attempt),
      20000,
      'The server took too long to answer.'
    );
    if (cancelled()) return;
    socket = ws;

    await capture.start();
    if (cancelled()) return;
    // The server can hang up while the browser is still asking about the mic.
    if (!socket || socket !== ws || socket.readyState !== WebSocket.OPEN) {
      throw new Error('The connection dropped while starting.');
    }

    recording = true;
    ui.micBtn.dataset.active = 'true';
    ui.micLabel.textContent = 'Stop';
    ui.hint.textContent = 'Just talk. Interrupt any time.';
    setState('listening', 'Listening');
  } catch (error) {
    if (!cancelled()) {
      toast(describeStartError(error), 'error');
      await stopRecording({ silent: true });
    }
  } finally {
    // A pending socket that never became `socket` (timeout, early close, a
    // cancelled start) is ours to close; the live one is stopRecording's. The
    // same goes for a mic that a stop never got to see.
    if (attempt.ws && attempt.ws !== socket) closeQuietly(attempt.ws);
    if (cancelled()) {
      if (capture && capture !== mic) capture.stop();
    } else {
      ui.micBtn.disabled = false;
    }
  }
}

async function stopRecording({ silent = false } = {}) {
  generation++; // cancels a start that is still waiting on something
  recording = false;
  ui.micBtn.dataset.active = 'false';
  ui.micBtn.disabled = !supported;
  ui.micLabel.textContent = 'Start talking';
  ui.hint.textContent = 'Click to start a conversation';

  // Take ownership and clear the module state before awaiting anything: a
  // start pressed while mic.stop() is still closing the context must find a
  // clean slate, not have its new mic and socket nulled underneath it.
  const stopping = mic;
  const closing = socket;
  mic = null;
  socket = null;

  if (closing && closing.readyState === WebSocket.OPEN) {
    closing.send('stop');
    closing.close();
  }
  if (gate) gate.cancel();
  if (player) player.flush();
  replyBuffer = '';
  clearLive();
  restIdle();
  if (!silent) loadHistory();
  if (stopping) await stopping.stop();
}

function toggleRecording() {
  if (recording) stopRecording();
  else startRecording();
}

// --- settings ----------------------------------------------------------------

function openSettings() {
  const keys = loadKeys();
  KEY_FIELDS.forEach((name) => {
    const field = el(`key-${name}`);
    if (field) field.value = keys[name] || '';
  });
  ui.settings.showModal();
}

function submitSettings(event) {
  event.preventDefault();
  const keys = {};
  KEY_FIELDS.forEach((name) => {
    const value = el(`key-${name}`).value.trim();
    if (value) keys[name] = value;
  });

  const missing = KEY_FIELDS.filter((name) => !keys[name]);
  if (KEYS_REQUIRED && missing.length) {
    toast('Deepgram, Ollama and Murf keys are all required here.', 'error');
    return;
  }

  if (!saveKeys(keys)) return;
  ui.settings.close();
  if (!recording) restIdle();
  toast(
    Object.keys(keys).length
      ? 'Saved on this device.'
      : 'Cleared. Falling back to the server keys.'
  );
  if (recording) toast('New keys apply next time you start.');
}

function forgetKeys() {
  KEY_FIELDS.forEach((name) => {
    const field = el(`key-${name}`);
    if (field) field.value = '';
  });
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

document.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' || event.target !== document.body) return;
  event.preventDefault();
  // Holding the key repeats keydown; each repeat would toggle the session.
  if (event.repeat) return;
  // A click on a disabled button is swallowed by the browser; this path is not,
  // so without the check a second press during "Connecting" would start a
  // second session and orphan the first microphone stream and socket.
  if (ui.micBtn.disabled) return;
  toggleRecording();
});

window.addEventListener('beforeunload', () => {
  if (socket) socket.close();
});

const support = detectSupport();
if (!support.ok) {
  supported = false;
  ui.micBtn.disabled = true;
  ui.hint.textContent = support.reason;
}

sessionId();
loadHistory();
restIdle();
if (supported && missingKeys().length) openSettings();
