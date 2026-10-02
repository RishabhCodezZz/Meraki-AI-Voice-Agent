# CLAUDE.md — Meraki Voice Agent

Guidance for Claude Code in this repo. Keep it current as the project changes.

---

## 1. What this is

Live at https://meraki-ai-voice-agent.onrender.com (free instance, sleeps after
15 minutes idle; a cold start takes 30-60s and Render's edge answers 404 with
`x-render-routing: no-server` while the container boots, rather than holding the
request — that is normal and not a failed deploy).

A real-time voice agent. Rewritten from scratch in v3.0.0 (see §6 for what the
original looked like and why none of it survived); now v3.1.0 after the
hardening branch (§10). The version lives in `meraki/__init__.py` and nowhere
else.

```
mic ──► PCM16 @16kHz ──► Deepgram Nova-3 ──► Ollama Cloud ──► Murf ──► speakers
        (AudioWorklet)      streaming STT      streaming LLM    chunked TTS
                    └──────────── one WebSocket ────────────┘
```

## 2. Layout

| Path | Role |
|---|---|
| `meraki/__init__.py` | `__version__`, the only place the version is written; `config.APP_VERSION` re-exports it |
| `meraki/main.py` | FastAPI app, HTTP routes, `_Connection` (one per browser), echo filter |
| `meraki/pipeline.py` | `TurnPipeline` — one cancellable conversational turn |
| `meraki/session.py` | `SessionStore` — history, LRU + TTL, in memory |
| `meraki/protocol.py` | WebSocket message constructors; the wire contract |
| `meraki/security.py` | WebSocket Origin check; ASGI middleware for security headers and `/static` revalidation |
| `meraki/config.py` | Settings, model/voice lists, `ApiKeys`, system prompt |
| `meraki/services/stt.py` | Deepgram streaming WebSocket |
| `meraki/services/llm.py` | Ollama Cloud NDJSON streaming |
| `meraki/services/tts.py` | Murf; chunking + pipelined synthesis |
| `static/js/app.js` | UI wiring and state machine: start/stop/connect, banner, key dialog |
| `static/js/audio-capture.js` | getUserMedia → AudioWorklet → PCM16 |
| `static/js/audio-player.js` | `SpeechPlayer`: gapless scheduled playback, serialised decoding, flushable |
| `static/js/playback-gate.js` | `PlaybackGate`: the one place that decides "Speaking" has really ended |
| `static/js/support.js` | Pure helpers: `detectSupport`, `describeStartError`, `withTimeout`, `retryWithin`, `shouldRetryConnect`, `makeSessionId`, `isValidSessionId`, `pickKeys`, `isMicBlocked`, `endsTurn` |
| `static/js/visualizer.js` | The bar meter |
| `static/js/worklets/capture-processor.js` | Runs on the audio thread |
| `requirements.txt` | Runtime dependencies, pinned |
| `requirements-dev.txt` | Runtime plus `pytest` and `httpx2` (what Starlette's `TestClient` imports) |
| `render.yaml` | Render Blueprint: start command (with `--ws-max-size`), health check, Python version |
| `tests/` | Offline unit tests; no keys needed. Barge-in, echo, TTS delivery and request shape, pipeline, STT turns, connection, handshake/HTTP hardening |
| `tests/frontend/` | Browser-logic tests via `node --test`, no dependencies: capture, player, gate, support, visualizer |
| `.github/workflows/ci.yml` | Runs both suites plus a boot check on every push to main and every pull request |

## 3. Running

```bash
pip install -r requirements-dev.txt       # runtime deps + pytest + httpx2
python run.py                             # http://127.0.0.1:8000
python -m pytest tests/ -q                # backend
node --test "tests/frontend/*.test.js"    # browser logic, needs no npm install
```

`requirements.txt` alone is enough to run the app; the backend tests also need
the dev file, because Starlette 1.x's `TestClient` imports `httpx2`.

`.claude/launch.json` defines a `meraki` preview server (with `--reload` and
`--ws-max-size 65536`) for use with `preview_start`.

## 4. Architecture facts — read before changing anything

- **Keys are bring-your-own, resolved per connection.** `ApiKeys.from_payload`
  takes whatever the browser sent and falls back to the environment for anything
  omitted, so a visitor's key beats the server's and a blank field does not blank
  a working one. The result lives on `_Connection` and is frozen. **Never assign
  keys to module state** — one global dict is exactly the cross-user leak in §6,
  and `test_no_module_holds_keys_of_its_own` guards against it returning.
- **The page knows whether it must demand keys.** `keys_required` is passed to
  the template from `ApiKeys.from_env().missing()` and rendered as
  `<body data-keys-required="true|false">`, so a deployment with its own keys
  does not shove a dialog at first-time visitors. `app.js` treats anything but
  `'false'` as required. It is a data attribute, not an inline script, because
  the CSP has no `unsafe-inline`.
- **The deployment deliberately sets no keys** (`render.yaml` declares none), so
  it costs nothing to run and every visitor spends their own free tier. Missing
  all three logs at INFO, not WARNING — that is the intended posture. *Partially*
  configured logs a warning, because it is nearly always a mistake.
- **Deploy via Blueprint, not the manual web-service form.** The form pre-fills
  `gunicorn your_application.wsgi`, which cannot run an ASGI app, and its health
  check placeholder is `/healthz` where this serves `/health`. `render.yaml` gets
  all of it right; changing it and pushing updates the deployment.
- **`ready` means everything is up**, including the Deepgram socket. It is sent
  after STT connects, not during the handshake — otherwise the browser goes and
  asks for microphone permission before we know the STT key is even valid. A
  Deepgram 401/403 goes out as error code `keys` (not `stt`) so the browser
  offers Open Keys rather than a Retry that can only fail the same way.
- **The handshake is untrusted input.** `_handshake` turns every bad first frame
  (binary, not JSON, nested deep enough to overflow the parser, not an object,
  not `config`, or later than 15 s) into a typed fatal `handshake` error;
  `keys` that is not an object counts as no keys. `session_id` is kept only if
  it matches `[A-Za-z0-9_-]{8,64}` (`session.py`), otherwise the server mints a
  `uuid4().hex`. There is deliberately no shared fallback: the old
  `"anonymous"` put everyone who sent no id into one conversation. The history
  routes ignore invalid ids. The client applies the same rule
  (`isValidSessionId`, `makeSessionId`), so a `?s=` the server would refuse is
  replaced up front instead of loading history that never resumes.
- **The WebSocket checks Origin.** Browsers do not apply the same-origin policy
  to WebSockets, so any page could dial `/ws` from a visitor's browser. It could
  not read this origin's localStorage, so the visitor's keys are safe; what it
  could spend is the server's own environment keys, and the check stops other
  websites' pages from using visitors' browsers to do that.
  `origin_allowed` (`security.py`) refuses the upgrade with close 1008, before
  `accept()`, unless the Origin's host equals the request's `Host` or the whole
  origin is listed in `MERAKI_ALLOWED_ORIGINS` (comma-separated, for a front end
  hosted elsewhere). No Origin header passes - curl and the tests are not
  hijacked browsers. The comparison relies on Render's proxy handing the app
  the Host unchanged; that has not been checked live (§9).
- **Bounded on the way in and out.** `--ws-max-size 65536` (in `render.yaml`,
  `run.py` and `launch.json`): mic frames are a few KiB and the default is
  16 MiB. The shared aiohttp session uses `TCPConnector(limit=400)` (the
  default of 100 caps the app at roughly 100 visitors, one Deepgram socket
  each) and `sock_connect=10`, with `total=None` so a long reply is never cut
  off (`llm.py` and `tts.py` set their own per-read timeouts).
- **A turn is one cancellable task.** `_Connection._turn`. Barge-in cancels it,
  which unwinds the LLM request and any in-flight synthesis. `TurnPipeline.run`
  records the partial reply in its `finally`, so an interrupted answer is still
  remembered. `TurnPipeline` wraps `stream_speech` in `aclosing`, so a cancel
  that lands mid-send closes the generator at once instead of leaving its
  producer to send a `reply_chunk` after `interrupted`. `_cancel_turn` waits
  with `asyncio.wait`, not `await turn` under `suppress(CancelledError)`, which
  would also swallow a cancel aimed at the caller; `close()` always reaches
  `ws.close()`. `close()` stops the pump *before* the turn: the pump is the only
  thing that starts turns, and it does so synchronously after its own
  `_cancel_turn`, so once it is gone any turn is already in `self._turn`. The
  other order let a `final` arriving during teardown start a turn nobody
  cancelled, which then spent quota and wrote history after the socket closed.
  Logs carry only the first six characters of a session id (`_short_id`); the
  full id is a bearer token for the history routes.
- **Barge-in is two cuts that agree.** Server: a partial of
  ≥ `BARGE_IN_MIN_WORDS` (2) words, or any final that is not echo, calls
  `_cancel_turn(notify=True)`, which sends `interrupted` when a turn was
  running *or* audio is still audibly playing (`_audio_pending` and
  `_echo_active()`), once per utterance (`_interrupt_sent`, cleared when the
  final starts a turn). The turn task ends when synthesis does, seconds before
  the browser has played it out, so "nothing running" does not mean "nothing to
  cut off". Client: `handleMessage` flushes the player itself on a ≥ 2-word
  partial while playing, on any final, and on `thinking`, rather than waiting a
  round trip for `interrupted`. One word on a partial is too twitchy against
  residual echo; waiting for a final is too slow to feel like an interruption.
- **Meraki's own voice is filtered by text, not by muting.** `looks_like_echo`
  drops a transcript only if it is a run of whole words contained in what is
  being spoken and has at least `ECHO_MIN_WORDS` (2) of them: "no" is not found
  inside "know", and a bare "yes" or "no" always gets through. It is consulted
  only while `_echo_active()`: `ECHO_GRACE_SECONDS` (2 s) from `thinking`, then,
  once audio starts, until `len(spoken) / ECHO_CHARS_PER_SECOND` (12) plus the
  grace. Without that expiry `_spoken` lingered until the next turn and a phrase
  said back minutes after a reply was swallowed. `_spoken` is still held past
  the end of a turn on purpose (trailing audio echoes too) and reset on
  `thinking`. Tests control the clock through `main._now`. Muting the mic during
  playback would also work and would remove barge-in, which is the wrong trade.
- **Deepgram's two flags are not interchangeable.** `is_final` means a segment is
  settled; `speech_final` means endpointing fired. Segments accumulate and the
  utterance is emitted on `speech_final`. Acting on `is_final` alone chops long
  sentences into fragments.
- **Everything is asyncio.** No threads anywhere. Do not add blocking I/O to an
  `async def` — `aiohttp` or `asyncio.to_thread`.
- **The mic is never connected to `destination`.** The capture worklet has
  `numberOfOutputs: 0`. Connecting it to the speakers creates a feedback path
  into the transcriber.
- **Every promise in `connect()` must settle.** A fatal error can arrive before
  `ready` (a bad Deepgram key does exactly this). Leaving the promise pending
  hangs `startRecording` with the mic button stuck disabled - that was a real
  regression. Nothing automated covers it now: `connect` lives in `app.js`,
  which has no unit tests (§9).
- **TTS chunk thresholds are floors, not targets.** `FIRST_CHUNK_MIN_CHARS` (12)
  is where we *start looking* for a boundary, so a short opener ships
  immediately. Raising it directly increases time-to-first-audio.
- **`stream_speech` delivers a chunk as soon as it and every earlier one are
  done.** A producer task pulls text and starts synthesis (at most
  `MAX_IN_FLIGHT`, 3, via a semaphore); the consumer takes the tasks from a
  queue in order. It used to yield only once three tasks were queued or the text
  ended, so one- and two-chunk replies waited for the whole LLM reply, and the
  text side stalled because nothing pulled it while the consumer awaited audio.
  Cancelling the consumer cancels the producer and every synth task, and closes
  the text stream.
- **The style fallback is per `(voice, model)`, and only on a 400 whose body
  mentions `style`** (`_style_rejected`). It was a process-global flag flipped
  by any Murf 400, so one unrelated rejection (out of credits) degraded every
  visitor's voice until restart.
- **Only the first chunk cuts on a clause** (`allow_clause`). The persona asks
  for one-sentence replies, so a sentence-only rule meant the single boundary
  was at the very end and pipelining never engaged. Measured 4.0s → 3.2s to
  first audio. Do not "tidy" this into a uniform rule.

## 5. Conventions

- WebSocket frames are JSON with a `type` discriminator. The full contract lives
  in the `protocol.py` docstring — update it there when adding a message type.
- Upstream errors become typed `error` frames with a human-readable message.
  Never send synthetic or placeholder audio; the browser cannot tell it from
  real audio and will hang waiting for playback that never ends.
- Never commit `.env` (gitignored). `.env.example` documents the variables.
- Frontend is ES modules, no build step, no framework. The only external
  dependency is Google Fonts (Space Grotesk + JetBrains Mono); everything else
  is served locally.
- **Security headers are middleware, and the CSP is strict.**
  `SecurityHeadersMiddleware` adds a CSP, `nosniff`, `no-referrer` and a
  microphone-only Permissions-Policy to every HTTP response, only where the
  route has not set its own, except 500 responses produced by Starlette's
  `ServerErrorMiddleware`. Both middleware in `security.py` are plain ASGI
  rather than `BaseHTTPMiddleware`, which wraps the response in a task and queue
  and sits in front of the WebSocket for no benefit. The CSP has no
  `unsafe-inline`, so the template has no inline `<script>` or `<style>`; a new
  external origin must be added to `CONTENT_SECURITY_POLICY` or the browser
  blocks it without a server-side trace. `/` and `/health` answer HEAD as well
  as GET, since uptime checks probe with HEAD and a 405 reads as down.
- **Only `styles.css` and `app.js` carry `?v={{ asset_v }}`**, a token derived
  from the newest mtime under `static/`. Without it the browser keeps its cached
  CSS and JS, so a deploy ships new markup against old styles — which is exactly
  what happened during the redesign and looked like the CSS being broken. The
  token is recomputed at most every `ASSET_VERSION_TTL` (2 s): cached for the
  process lifetime it went stale under `uvicorn --reload`, which only watches
  `*.py`. Everything else under `/static` (the ES modules `app.js` imports and
  the capture worklet) has an unversioned URL, so `StaticCacheMiddleware` sends
  `Cache-Control: no-cache` and the browser revalidates (a cheap 304) instead of
  reusing a stale copy for days. A new `<link>` or `<script>` in the template
  needs `?v=` too.
- **The status chip tells the truth about whether it can run.** `restIdle()` is
  the single place that decides between green "Ready" and red "Needs keys", and
  every path back to rest goes through it. Do not call `setState('idle', ...)`
  directly from a handler — that is how it ended up claiming Ready with no keys.
  `restIdle()` is a no-op while `starting` is set, because a start owns the chip
  until it ends.
- **A start is cancellable, and cancelling is decided on state, not on the
  chip.** `generation` is bumped by every start and every stop; a start that
  wakes from an await and finds it changed was cancelled and must put down what
  it made without touching state a newer session now owns. `starting` is true
  for the whole connect, retries and microphone prompt included, and the mic
  button reads "Cancel" while it is. Other handlers (Clear, saving keys) call
  `restIdle()` in the middle of a start, which is why Cancel cannot key off the
  chip's text. Both audio contexts are created before the first `await`, inside
  the click gesture; Safari and iOS refuse them otherwise.
- **Connecting retries network failures for 60 s.** Render's edge answers a
  sleeping container with an immediate 404, so the upgrade fails fast, over and
  over, for 30-60 s. `retryWithin` (`CONNECT_RETRY_DELAY_MS` 3 s,
  `CONNECT_BUDGET_MS` 60 s) retries only errors marked `network`
  (`shouldRetryConnect`): never a server `error` frame, which says the same
  thing every time, and never by matching the message text. Fatal errors land in
  a persistent banner, not a toast: Retry, or Open Keys when the code is `keys`.
- **`PlaybackGate` is the single place that decides speaking has ended.** Two
  inputs arrive in either order: the server's `speech_done` (in the same burst
  as the last audio frame, before it has decoded) and the player running dry. It
  settles only when both have happened; the player's idle signal alone fires in
  every gap between chunks of one reply. `SpeechPlayer.enqueue` serialises
  decoding and `playing` includes `pending`, so a chunk still decoding counts as
  playing. "Stop speaking" sets `muteReply`, which drops `reply_chunk` and
  `audio` until the next `thinking`; the protocol has no message for it, so the
  server keeps going and `reply_done` still adds the reply text to the
  transcript. Clear is the exception: it also sets `discardReply`, so the cleared
  transcript is not refilled by the reply that was in flight. Only error codes in
  `TURN_ENDING_ERROR_CODES` (`llm`, `tts`, `network`, `internal`) end the gate's
  turn; a non-fatal `stt` error mid-reply just toasts. A `tts` failure sends
  `reply_done` before the `error`, so the text still reaches the transcript.
- **The live caption reserves its space** (`.live { min-height }`) and is never
  hidden. Toggling it shoved the whole page down the moment Meraki started
  speaking and back up when it stopped.
- **`GET /api/history` must not create.** It uses `sessions.peek`, not `get`.
  Creating on read meant requesting unknown ids conjured empty conversations and
  evicted real ones out of the LRU — 40 requests to random ids wiped everyone.
- **The speech pump must never die quietly.** `_drain_speech_events` wraps
  `_pump_events` and reports both a crash and an unexpected `closed` as a fatal
  frame. Without that the socket stays open, the browser keeps streaming audio,
  and the UI sits on "Listening" forever.
- **The page does not scroll, down to a point.** `body` is a five-row grid at
  `100dvh` with the conversation on `minmax(0, 1fr)`; that row plus
  `min-height: 0` on `.log` is what lets the transcript shrink and scroll
  internally while the mic stays on screen. Get the row count wrong and the
  `1fr` lands on the stage instead. On short viewports the headline is hidden at
  `max-height: 680px` to leave the transcript room, and at `max-height: 560px`
  the grid is released and the page scrolls, with a 220px minimum log.
- Use `textContent`, not `innerHTML`, for anything model- or user-derived.

## 6. What the rewrite fixed

The original was a single 590-line `app.py` plus one 390-line HTML file. Audited
2026-09-06; every item below was a real defect, and all are resolved.

**Was broken**
- `app.py` did not compile — `while True:z`, a stray keystroke.
- Chat history never displayed: the client used a URL session id, the server
  keyed history by a per-connection `uuid4()`. They never matched.
- API keys lived in one module-level global shared by every visitor, so the last
  person to save keys owned everyone's conversations and paid for them.

**Was slow**
- "Streaming audio" was not streaming. The whole reply was collected, sent to
  Murf's REST API in one request, the full MP3 downloaded, then played.
- `requests.post`/`get` and a synchronous Gemini call ran inside `async def`,
  freezing the server for every connected user.
- No barge-in; talking over the assistant silently discarded your turn.

**Was fragile**
- `loop.create_task` called from the AssemblyAI SDK's worker thread — not
  thread-safe. Switching to Deepgram removed the thread entirely.
- On TTS failure it base64-encoded the string `MOCK_AUDIO_FOR_...` and sent it
  as `audio/mp3`; the browser hung on a corrupt blob.
- `ThreadPoolExecutor` never shut down; `chat_histories` grew without bound.
- Mic was wired to the speakers. `ScriptProcessorNode` (deprecated) on the main
  thread. `wss://` hardcoded, so local dev could never connect. No WebSocket
  error handling, so a rejected connection hung the UI forever.

**Was wrong**
- The news skill matched any message containing "latest" and then *discarded the
  user's question*, replacing it with a canned instruction. Query hardcoded to
  `q='AI'`. Removed entirely — it was never load-bearing.
- The persona prompt asked for witty remarks *and* 1-2 sentence voice replies
  with no rule for which wins, so the model padded. It also opened with "What's
  up doc?", which is Bugs Bunny, not Spider-Man. The persona is back by request
  (see §7); the conflict is now resolved explicitly in the prompt.
- README advertised streaming audio, persistent memory and secure key handling.
  None of the three were true.

## 7. Decisions and their reasons

- **Ollama Cloud over Gemini.** Free tier, and the `google-generativeai` SDK
  keys off process-global state (`genai.configure`), which cannot back
  per-connection keys safely. Ollama is a plain HTTP call with a per-request
  header.
- **Deepgram over AssemblyAI.** ~690 free hours vs 5, lower latency, and a plain
  WebSocket instead of a synchronous SDK — which is what let the threading layer
  go away. AssemblyAI measures better on realtime accuracy benchmarks; that was
  the trade.
- **Gemma 4 31B, locked (was Nemotron 3 Nano until 2026-10-02).** On a voice
  agent, time-to-first-token is heard directly as dead air. Nano was the fastest
  on the free tier when first measured (207 ms), but on 2026-10-02 it took 18-30 s
  to the first token on six runs out of six, so replies arrived after the user had
  already talked over them (a >= 2 word partial cancels the turn). `gemma4:31b` on
  the same key: ~0.5 s. `gpt-oss:20b` returned an empty reply; `glm-5.3-flash` and
  `deepseek-v4.1-flash` need paid credits. Re-measure before changing it again.
  Falcon was requested earlier but is not an Ollama Cloud model at all (and is
  TII's, not ours).
- **Model and voice are server-side, not user-selectable.** The page does not
  ask and the handshake ignores any `model` / `voice_id` a client sends. Change
  them with `MERAKI_MODEL` / `MERAKI_VOICE_ID` / `MERAKI_VOICE_STYLE`.
- **Murf's stream endpoint, not generate.** Measured on identical text: generate
  2865ms to return anything, stream 150-280ms to first byte. Falcon 2 is Murf's
  current TTS model and exists only on the stream endpoint - generate rejects it
  and accepts only the deprecated GEN2. Each chunk's MP3 is read to completion
  from the stream, base64-encoded by `synthesize` and sent as one `audio` frame;
  24 kHz mono halves the bytes against the 44.1 kHz default and speech does not
  need the headroom. `Conversational` style is what makes it sound friendly -
  more than the voice choice does - and an unsupported style is dropped and
  retried, once, remembered per `(voice, model)`.
- **No news/weather/tool APIs.** Every remaining key is load-bearing. Optional
  integrations were the source of the worst prompt bug in the original.
- **Spider-Man-flavoured persona, kept at the user's request.** The prompt names
  no character and quotes no dialogue - it describes a temperament. The rule that
  makes it work on voice is the tie-breaker: form beats personality, and a joke
  that costs a sentence gets cut. If replies start getting long, that line in
  `SYSTEM_PROMPT` is the first thing to check.

## 8. Verified against live APIs (2026-09-07)

Not inferred - actually run, against the code as it was on that date. The
2026-10-02 branch changed how chunks are delivered, the echo filter and the
dependencies, and none of this has been re-measured live since.

- Deepgram transcribed a 3.3s sample word-perfect, and `speech_final` fires once
  trailing silence arrives. Without trailing silence it never fires, and since a
  turn only starts on `speech_final`, a client that stops sending audio the
  instant the user stops talking would hang. The mic streams continuously, so
  this holds - but it is worth knowing.
- Ollama replied in 207ms with the persona intact (one sentence, no preamble).
- Murf's stream endpoint returns audio in 150-280ms against 2865ms for
  `/v1/speech/generate` on identical text.
- A full `TurnPipeline` run: 0.6s to first token, 3.2s to first audio.

## 9. Open items

- Murf's stream endpoint is consumed to completion per chunk. Forwarding its
  bytes to the browser as they arrive (PCM rather than MP3) would shave a few
  hundred ms more, at roughly 12x the bandwidth.
- History is in memory only; a restart loses it. Fine for a demo, needs Redis or
  similar for anything real.
- **Nothing on the 2026-10-02 branch has run against a real microphone and live
  keys.** The suites and a local boot pass; barge-in, the playback gate and the
  cold-start retry want a hands-on test, and so does a live `wss://` check on
  Render.
- **Render must be checked live for the Host header.** The Origin check
  compares the Origin's host to the `Host` header; if Render's proxy rewrites
  it, every browser connection is refused with 1008.
- The dependency bump (fastapi 0.142.2, starlette 1.7.0, uvicorn 0.54.0,
  aiohttp 3.14.3) is a major Starlette jump verified by both suites and a local
  boot only. `httpx2`, which `requirements-dev.txt` installs because Starlette's
  `TestClient` names it, has unconfirmed PyPI ownership; `pytest` and `httpx2`
  are pinned to ranges (`pytest>=8,<10`, `httpx2>=2.0,<3`). Dev-only.
- The pinned stack (fastapi 0.142.2 / starlette 1.7.0 / uvicorn 0.54.0 /
  aiohttp 3.14.3) was run green in a throwaway venv by the author of this branch;
  the PR's CI must confirm it, including the `startup` job.
- A refused Origin upgrade (close 1008) is indistinguishable from a sleeping
  server in a browser, so the client treats it as a network failure and retries
  it for the whole 60 s budget before showing the error.
- No echo cancellation beyond the browser's `echoCancellation: true` and the
  text filter. On speakers at volume, barge-in can still self-trigger when the
  transcript of the echo is not a contiguous run of the spoken words. A
  one-word echo *final* is not filtered (the 2-word floor lets a lone "no" or
  "yes" through) and so starts a turn.
- History records the text the model generated, not the text that was heard. An
  interrupted reply is stored up to the moment of cancel, which can be well
  ahead of what the browser had actually played.
- LLM output is not stripped of markdown before TTS. The persona prompt forbids
  it; nothing enforces it, so a stray asterisk is read aloud.
- Ollama and Murf key rejections arrive as non-fatal `llm` / `tts` errors, so
  the browser shows a toast with no Open Keys action. Only a Deepgram rejection
  is reported as code `keys`.
- `app.js` is ~880 lines. `connect`, `startRecording` and `stopRecording` (the
  generation / `starting` logic) have no unit tests; only the pure helpers in
  `support.js` and `PlaybackGate` do. The key dialog is the obvious piece to
  split out.
- A 500 from an unhandled exception is produced by Starlette's outermost
  `ServerErrorMiddleware`, outside ours, so it carries no security headers.
- The CSP's `connect-src` allows `ws:` and `wss:` to any host, wider than the one
  host the page needs.
- No rate limiting on the public deployment. The Origin check protects
  browsers only; a script with no Origin header can open `/ws`.
- `looks_like_echo` lives in `main.py`; if that file grows it wants its own home.

## 10. Changelog

- **2026-10-02** — Hardening and UI branch, v3.1.0. Backend tests 76 → 140
  and browser-logic tests 21 → 102 (242 in all). Voice: TTS chunks are
  delivered as they finish instead of waiting on the text stream; the style
  fallback is per `(voice, model)` and needs a 400 that mentions `style`; the
  echo filter matches whole words, needs two, and expires; `interrupted` covers
  audio still playing after the turn task ended; the client cuts playback itself
  on barge-in; `PlaybackGate` settles "Speaking" in one place; start/stop is
  race-free and Cancel works during a cold-start retry. Hardening: handshake
  validation with typed errors, session-id rule (no `"anonymous"`), Origin
  allow-list (`MERAKI_ALLOWED_ORIGINS`), `--ws-max-size`, connector limit and
  connect timeouts, CSP and other security headers, `no-cache` on `/static`,
  HEAD routes, asset token with a 2 s TTL, Deepgram rejection as code `keys`.
  UI: contrast, focus, live regions, short-viewport layout, guided key dialog,
  persistent error banner with Retry / Open Keys. Dependencies bumped; version
  now has one source. Docs reconciled with all of it. Final-review fix wave:
  `close()` stops the pump before the turn (no turn can start during teardown),
  only `llm`/`tts`/`network`/`internal` errors end the gate's turn, Clear mutes
  and discards the reply still in flight, `reply_done` precedes a `tts` error,
  no Retry on a blocked microphone, no layout shift for Stop speaking on phones,
  six-character session ids in logs, dev dependencies pinned to ranges.
- **2026-09-07** — Deployed. Verified live over `wss://`: keyless handshake,
  bogus-key rejection and malformed-frame handling all correct, ~1.1s round trip.

- **2026-09-06** — Audited the original. 3 P0, 7 P1, 17 P2 defects documented.
- **2026-09-07** — Final audit. Fixed: GET /api/history created sessions and
  evicted real ones; the speech pump could die silently leaving the UI stuck;
  a non-object handshake crashed with AttributeError; the spacebar bypassed the
  disabled mic button and could start two sessions. Removed an unused
  "interrupt" message the client never sent, corrected the protocol docstring,
  and guarded `roundRect` for Safari below 16.4. Tests 85 → 97.
- **2026-09-07** — Status chip now reflects whether keys exist (green Ready /
  red Needs keys) instead of always claiming Ready. Stopped the layout jumping
  while speaking, and closed the conversation card off the bottom edge.
- **2026-09-07** — Bring-your-own-key restored, per connection, with the server
  as fallback. Tightened the persona: replies were drifting into three sentences
  and trailing "anything else?" offers. Tests 78 → 85.
- **2026-09-07** — UI rebuilt: Space Grotesk + JetBrains Mono, technical dark
  treatment, chip chrome. Fixed the transcript scroll (the grid had four tracks
  for five children, so the free space went to the stage) and added asset
  versioning so cached CSS cannot outlive a deploy.
- **2026-09-07** — Murf's stream endpoint replaces generate: 3.2s → ~1.4s to
  first audio. Added CI (both suites plus a boot check) and 21 browser-logic
  tests via `node --test`. Tests 57 → 78.
- **2026-09-07** — Verified the whole pipeline against live keys. First chunk
  now cuts on clauses (4.0s → 3.2s to first audio). Adopted FastAPI's lifespan
  handler, removing 4 deprecation warnings from every test run.
- **2026-09-07** — Echo rejection: the assistant no longer interrupts itself on
  speakers. Removed the unused mic-mute plumbing that approach made redundant.
- **2026-09-07** — Keys moved to the environment; settings dialog and all
  client-side key handling removed while developing. BYO to return before
  hosting (it did, in the entry above).
- **2026-09-06** — Locked model and voice server-side (pickers removed). Murf
  now returns inline base64 at 24 kHz with the Conversational style, cutting a
  round trip per chunk. Fixed a hang where a pre-`ready` fatal error left the
  mic button permanently disabled. Tests 19 → 46.
- **2026-09-06** — Restored the Spider-Man persona, rewritten so wit and voice
  brevity no longer conflict. Accent amber → red, spider brand mark, tagline
  back. White-on-red button contrast checked at 5.3:1 (AA).
- **2026-09-06** — v3.0.0 rewrite. Renamed to Meraki. Gemini → Ollama Cloud,
  AssemblyAI → Deepgram, NewsAPI removed. Per-connection keys, real pipelined
  TTS, barge-in, AudioWorklet capture, gapless playback, new dark-studio UI,
  modular backend, 19 tests. Verified: page renders, save flow works, mic-denial
  and all three WebSocket error paths surface correctly.
