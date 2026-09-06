import { MicCapture } from './audio-capture.js';
import { SpeechPlayer } from './audio-player.js';
import { Visualizer } from './visualizer.js';

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

// --- chrome ------------------------------------------------------------------

let uiState = 'idle';

function setState(state, label) {
  uiState = state;
  visualizer.setState(state);
  ui.status.textContent = label;
  ui.statusDot.dataset.state = state;
  document.body.dataset.state = state;
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
  ui.transcript.append(row);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

function showLive({ user, reply }) {
  if (user !== undefined) ui.liveUser.textContent = user;
  if (reply !== undefined) ui.liveReply.textContent = reply;
  const hasContent = Boolean(ui.liveUser.textContent || ui.liveReply.textContent);
  ui.live.hidden = !hasContent;
}

function clearLive() {
  ui.liveUser.textContent = '';
  ui.liveReply.textContent = '';
  ui.live.hidden = true;
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
      if (!player.playing) setState(recording ? 'listening' : 'idle', recording ? 'Listening' : 'Ready');
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
      else setState(recording ? 'listening' : 'idle', recording ? 'Listening' : 'Ready');
      break;
  }
}

// --- recording ---------------------------------------------------------------

async function startRecording() {
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
  setState('idle', 'Ready');
  if (!silent) loadHistory();
}

function toggleRecording() {
  if (recording) stopRecording();
  else startRecording();
}

// --- boot --------------------------------------------------------------------

ui.micBtn.addEventListener('click', toggleRecording);
ui.clearBtn.addEventListener('click', clearHistory);

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
setState('idle', 'Ready');
