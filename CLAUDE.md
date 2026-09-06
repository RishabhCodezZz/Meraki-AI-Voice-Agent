# CLAUDE.md — Meraki Voice Agent

Guidance for Claude Code in this repo. Keep it current as the project changes.

---

## 1. What this is

A real-time voice agent. Rewritten from scratch in v3.0.0 (see §6 for what the
original looked like and why none of it survived).

```
mic ──► PCM16 @16kHz ──► Deepgram Nova-3 ──► Ollama Cloud ──► Murf ──► speakers
        (AudioWorklet)      streaming STT      streaming LLM    chunked TTS
                    └──────────── one WebSocket ────────────┘
```

## 2. Layout

| Path | Role |
|---|---|
| `meraki/main.py` | FastAPI app, HTTP routes, `_Connection` (one per browser) |
| `meraki/pipeline.py` | `TurnPipeline` — one cancellable conversational turn |
| `meraki/session.py` | `SessionStore` — history, LRU + TTL, in memory |
| `meraki/protocol.py` | WebSocket message constructors; the wire contract |
| `meraki/config.py` | Settings, model/voice lists, `ApiKeys`, system prompt |
| `meraki/services/stt.py` | Deepgram streaming WebSocket |
| `meraki/services/llm.py` | Ollama Cloud NDJSON streaming |
| `meraki/services/tts.py` | Murf; chunking + pipelined synthesis |
| `static/js/app.js` | UI wiring and state machine |
| `static/js/audio-capture.js` | getUserMedia → AudioWorklet → PCM16 |
| `static/js/audio-player.js` | Gapless scheduled playback, flushable |
| `static/js/visualizer.js` | The bar meter |
| `static/js/worklets/capture-processor.js` | Runs on the audio thread |
| `tests/` | Offline unit tests; no keys needed |

## 3. Running

```bash
pip install -r requirements.txt
python run.py                  # http://127.0.0.1:8000
python -m pytest tests/ -q
```

`.claude/launch.json` defines a `meraki` preview server (with `--reload`) for
use with `preview_start`.

## 4. Architecture facts — read before changing anything

- **Keys come from the environment**, read at handshake via `ApiKeys.from_env()`.
  Bring-your-own-key is temporarily off while developing; the browser is not
  asked and any `keys` it sends are ignored. Restoring BYO means bringing back
  `from_payload` and the settings dialog, both in git history — and it must stay
  per-connection, never module-level, or the cross-user leak in §6 returns.
  While BYO is off, every visitor spends the deploy owner's credits.
- **`ready` means everything is up**, including the Deepgram socket. It is sent
  after STT connects, not during the handshake — otherwise the browser goes and
  asks for microphone permission before we know the STT key is even valid.
- **A turn is one cancellable task.** `_Connection._turn`. Barge-in cancels it,
  which unwinds the LLM request and any in-flight synthesis. `TurnPipeline.run`
  records the partial reply in its `finally`, so an interrupted answer is still
  remembered.
- **Barge-in fires on a partial of ≥ `BARGE_IN_MIN_WORDS` (2)**, not on finals.
  One word is too twitchy against residual echo; waiting for a final is too slow
  to feel like an interruption.
- **Meraki's own voice is filtered by text, not by muting.** `looks_like_echo`
  drops any transcript contained in what is currently being spoken. Muting the
  mic during playback would also work and would remove barge-in, which is the
  wrong trade. `_spoken` is held past the end of a turn on purpose — trailing
  audio echoes too — and reset when the next turn starts.
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
  regression, fixed and covered by the smoke script.
- **TTS chunk thresholds are floors, not targets.** `FIRST_CHUNK_MIN_CHARS` (12)
  is where we *start looking* for a boundary, so a short opener ships
  immediately. Raising it directly increases time-to-first-audio.
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
- Frontend is ES modules, no build step, no framework, no CDN dependencies.
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
- **Nemotron 3 Nano, locked.** On a voice agent, time-to-first-token is heard
  directly as dead air, and Nano is the fastest model on Ollama Cloud's free
  tier. Falcon was requested but is not an Ollama Cloud model at all (and is
  TII's, not ours), so there was nothing faster to move to.
- **Model and voice are server-side, not user-selectable.** The page does not
  ask and the handshake ignores any `model` / `voice_id` a client sends. Change
  them with `MERAKI_MODEL` / `MERAKI_VOICE_ID` / `MERAKI_VOICE_STYLE`.
- **Murf `encodeAsBase64` + 24 kHz.** Inline audio removes a second round trip
  per chunk; 24 kHz halves the bytes and speech does not need the headroom.
  `Conversational` style is what makes it sound friendly - more than the voice
  choice does - and an unsupported style is dropped and retried, once, cached.
- **No news/weather/tool APIs.** Every remaining key is load-bearing. Optional
  integrations were the source of the worst prompt bug in the original.
- **Spider-Man-flavoured persona, kept at the user's request.** The prompt names
  no character and quotes no dialogue - it describes a temperament. The rule that
  makes it work on voice is the tie-breaker: form beats personality, and a joke
  that costs a sentence gets cut. If replies start getting long, that line in
  `SYSTEM_PROMPT` is the first thing to check.

## 8. Verified against live APIs (2026-09-07)

Not inferred - actually run:

- Deepgram transcribed a 3.3s sample word-perfect, and `speech_final` fires once
  trailing silence arrives. Without trailing silence it never fires, and since a
  turn only starts on `speech_final`, a client that stops sending audio the
  instant the user stops talking would hang. The mic streams continuously, so
  this holds - but it is worth knowing.
- Ollama replied in 207ms with the persona intact (one sentence, no preamble).
- Murf returned inline base64 with `Conversational` at 24kHz, confirming
  `encodeAsBase64` works and removing the download round trip.
- A full `TurnPipeline` run: 0.6s to first token, 3.2s to first audio.

## 8. Open items

- Murf REST is called once per chunk. If Murf's WebSocket streaming API is
  usable, it would cut a round trip per chunk. `tts.py` has the seam for it.
- History is in memory only; a restart loses it. Fine for a demo, needs Redis or
  similar for anything real.
- No echo cancellation beyond the browser's `echoCancellation: true`. On
  speakers at volume, barge-in can still self-trigger.
- No rate limiting or CORS policy on the public deployment.
- Frontend has no tests.

## 9. Changelog

- **2026-09-06** — Audited the original. 3 P0, 7 P1, 17 P2 defects documented.
- **2026-09-07** — Verified the whole pipeline against live keys. First chunk
  now cuts on clauses (4.0s → 3.2s to first audio). Adopted FastAPI's lifespan
  handler, removing 4 deprecation warnings from every test run.
- **2026-09-07** — Echo rejection: the assistant no longer interrupts itself on
  speakers. Removed the unused mic-mute plumbing that approach made redundant.
- **2026-09-07** — Keys moved to the environment; settings dialog and all
  client-side key handling removed while developing. BYO to return before
  hosting.
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
