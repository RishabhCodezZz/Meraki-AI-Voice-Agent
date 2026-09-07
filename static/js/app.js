import { MicCapture } from './audio-capture.js';
import { SpeechPlayer } from './audio-player.js';
import { Visualizer } from './visualizer.js';

const KEY_FIELDS = ['deepgram', 'ollama', 'murf'];
const STORAGE_KEYS = 'meraki.keys';
// The server tells us whether it has keys of its own. If it does, visitors can
// just talk; if not, they must bring their own.
const KEYS_REQUIRED = window.MERAKI_KEYS_REQUIRED !== false;

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
let recording = false;
let replyBuffer = '';

// --- session -----------------------------------------------------------------

function sessionId() {
  const url = new URL(window.location.href);
  let id = url.searchParams.get('s');
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
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
  if (missingKeys().length) setState('blocked', 'Needs keys');
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
  await fetch(`/api/history/${encodeURIComponent(sessionId())}`, { method: 'DELETE' });
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

function connect() {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(socketUrl());
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
      try {
        ws.close();
      } catch {
        /* already closing */
      }
      reject(new Error(message || 'Could not reach the server.'));
    };

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: 'config',
          session_id: sessionId(),
          keys: loadKeys(),
        })
      );
    };

    ws.onmessage = (event) => {
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
    ws.onclose = (event) => {
      failed('The server closed the connection.');
      if (recording) {
        stopRecording({ silent: true });
        if (!event.wasClean) toast('Connection lost.', 'error');
      }
      socket = null;
    };
  });
}

function handleMessage(message) {
  switch (message.type) {
    case 'partial':
      showLive({ user: message.text });
      break;

    case 'final':
      addTurn('user', message.text);
      showLive({ user: '', reply: '' });
      replyBuffer = '';
      break;

    case 'thinking':
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
      if (!player.playing) {
        if (recording) setState('listening', 'Listening');
        else restIdle();
      }
      break;

    case 'interrupted':
      player.flush();
      replyBuffer = '';
      showLive({ reply: '' });
      setState('listening', 'Listening');
      break;

    case 'error':
      toast(message.message, 'error');
      if (message.fatal) stopRecording({ silent: true });
      else if (recording) setState('listening', 'Listening');
      else restIdle();
      break;
  }
}

// --- recording ---------------------------------------------------------------

async function startRecording() {
  if (missingKeys().length) {
    toast('Add your API keys to get started.', 'error');
    openSettings();
    return;
  }

  setState('connecting', 'Connecting');
  ui.micBtn.disabled = true;

  try {
    player = player || new SpeechPlayer({
      onLevel: (bins) => visualizer.setSpectrum(bins),
      onIdle: () => {
        if (recording) setState('listening', 'Listening');
      },
    });
    // Unlock playback inside the click gesture, for Safari's autoplay policy.
    await player.ensureContext();

    socket = await connect();

    mic = new MicCapture({
      onFrame: (buffer) => {
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(buffer);
      },
      onLevel: (bins) => {
        if (uiState === 'listening') visualizer.setSpectrum(bins);
      },
    });
    await mic.start();

    recording = true;
    ui.micBtn.dataset.active = 'true';
    ui.micLabel.textContent = 'Stop';
    ui.hint.textContent = 'Just talk. Interrupt any time.';
    setState('listening', 'Listening');
  } catch (error) {
    const message =
      error && error.name === 'NotAllowedError'
        ? 'Microphone access was blocked.'
        : (error && error.message) || 'Could not start.';
    toast(message, 'error');
    await stopRecording({ silent: true });
  } finally {
    ui.micBtn.disabled = false;
  }
}

async function stopRecording({ silent = false } = {}) {
  recording = false;
  ui.micBtn.dataset.active = 'false';
  ui.micLabel.textContent = 'Start talking';
  ui.hint.textContent = 'Click to start a conversation';

  if (mic) {
    await mic.stop();
    mic = null;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send('stop');
    socket.close();
  }
  socket = null;
  if (player) player.flush();
  clearLive();
  restIdle();
  if (!silent) loadHistory();
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
  if (event.code === 'Space' && event.target === document.body) {
    event.preventDefault();
    toggleRecording();
  }
});

window.addEventListener('beforeunload', () => {
  if (socket) socket.close();
});

sessionId();
loadHistory();
restIdle();
if (missingKeys().length) openSettings();
