# Meraki

**[Try it live →](https://meraki-ai-voice-agent.onrender.com)** &nbsp;·&nbsp;
[![CI](https://github.com/RishabhCodezZz/Meraki-AI-Voice-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RishabhCodezZz/Meraki-AI-Voice-Agent/actions/workflows/ci.yml)

> **To try the demo you need your own API keys** for Deepgram, Ollama and Murf.
> All three have a free tier, and the dialog that opens on your first visit links
> to each signup page. The keys stay in your browser; the server holds them only
> for the length of your connection.
>
> It is hosted on a free instance that sleeps after 15 minutes idle. The first
> load can take 30–60 seconds to wake, and the page keeps retrying for up to a
> minute ("Waking the server…") before it gives up. It is not broken, it is
> yawning.

Your friendly neighbourhood AI — a real-time voice agent that talks back before
it has finished thinking.

Meraki has the temperament of a certain web-slinger: quick, warm, a bit of a
smart-arse, and completely serious the second it actually matters. The persona
is tuned for speech, so the wit lives in word choice rather than word count — if
a joke costs a whole sentence, the prompt tells it to cut the joke.

```
mic ──► PCM16 @16kHz ──► Deepgram Nova-3 ──► Ollama Cloud ──► Murf ──► speakers
        (AudioWorklet)      streaming STT      streaming LLM    chunked TTS
                    └──────────── one WebSocket ────────────┘
```

## Why it feels fast

Most hobby voice agents wait for the whole reply, synthesise it in one request,
then play it. That's several seconds of silence after every question.

Meraki pipelines instead. Reply text is cut into clause-sized chunks as it
streams out of the model — the first chunk deliberately short — and up to three
chunks are synthesised concurrently while the model keeps writing. Each one is
sent to the browser as soon as it and every earlier chunk are ready, without
waiting for more text. Chunks are scheduled on the Web Audio clock in order, so
playback is gapless. You hear the first words while the last ones are still
being generated.

It also listens while it speaks. Talk over Meraki — two or more words — and the
browser stops playing at once, the server cancels the in-flight turn mid-request
(even a reply whose text finished long ago but is still being spoken), and it
starts listening to you instead. Meraki's own voice coming back through your
speakers is recognised by matching the transcript against what it is saying, so
it mostly does not interrupt itself (on speakers at volume it can still
self-trigger; see CLAUDE.md open items); a lone "yes" or "no" is never mistaken
for echo.

## Running it yourself

```bash
git clone https://github.com/RishabhCodezZz/Meraki-AI-Voice-Agent.git
cd Meraki-AI-Voice-Agent
pip install -r requirements.txt
python run.py
```

Then open **http://127.0.0.1:8000**.

### Keys

Three services, each load-bearing: Deepgram listens, Ollama thinks, Murf speaks.

| Key | From | Free tier |
|---|---|---|
| Deepgram | [console.deepgram.com/signup](https://console.deepgram.com/signup) | $200 credit, roughly 690 hours |
| Ollama | [ollama.com/settings/keys](https://ollama.com/settings/keys) | covers the model below |
| Murf | [murf.ai](https://murf.ai/api/docs/introduction/overview) | trial credits |

There are two ways to supply them, and they compose:

**In the browser.** Click **Keys**, paste, save. They are stored in that
browser's `localStorage` and sent once, in the opening frame of that visitor's
own WebSocket. The server holds them on the connection object and writes them
nowhere, so two people using the same deployment can never see or spend each
other's credits.

**On the server.** Copy `.env.example` to `.env` (or set them in Render's
dashboard) and any key a visitor leaves blank falls back to yours.

A visitor's key always beats the server's, so someone who brings their own
spends their own quota.

**The hosted version sets no server keys**, so it costs nothing to run and every
visitor uses their own free tier. The dialog opens by itself on first visit and
links to all three signup pages.

Deployed from `render.yaml` as a Render Blueprint — the start command, health
check and Python version all come from the repo rather than a dashboard form.

Press **Start talking** and speak. Interrupt it whenever you like — it stops. The
button reads **Cancel** while it connects, and **Stop speaking** cuts a reply off
without ending the conversation.

## Model and voice

Both are fixed server-side and cannot be changed from the browser — the page
never asks, and anything a client sends for them is ignored.

- **`gemma4:31b`** on Ollama Cloud. Time to first token is the property that
  matters here: it is heard directly as dead air. On 2026-10-02 this model
  answered in about 0.5 s on the free tier while `nemotron-3-nano:30b`, the
  original choice, took 18–30 s. Requests send `think: false`, because a
  reasoning model's reasoning tokens arrive before anything speakable and buy
  nothing when the output is audio.
- **`en-US-natalie` with the `Conversational` style.** The style does more for
  how friendly it sounds than the choice of voice does; it is the difference
  between someone talking and someone reading. If a voice ever rejects the
  style (a 400 that names it), synthesis retries once without it and remembers
  that for that voice and model, rather than failing the turn.

Override either with `MERAKI_MODEL` / `MERAKI_VOICE_ID` / `MERAKI_VOICE_STYLE`
in the environment.

The WebSocket only accepts connections from the page's own host. To serve the
front end from somewhere else, list its origin in `MERAKI_ALLOWED_ORIGINS`
(comma-separated, e.g. `https://app.example.org`).

## Layout

```
meraki/
  __init__.py   the version, written once
  main.py       FastAPI app, WebSocket connection handling, echo filter
  pipeline.py   one conversational turn, cancellable for barge-in
  session.py    conversation history (LRU + TTL, in memory)
  protocol.py   the WebSocket message contract
  security.py   Origin check, security headers, static revalidation
  config.py     settings, model lists, the persona
  services/
    stt.py      Deepgram streaming
    llm.py      Ollama streaming
    tts.py      Murf, chunked and pipelined
static/js/
  app.js              wiring and UI state
  audio-capture.js    mic → PCM16
  audio-player.js     gapless scheduled playback
  playback-gate.js    decides when "Speaking" has really ended
  support.js          feature detection, errors, retry, session ids
  visualizer.js       the meter
  worklets/           capture runs on the audio thread
tests/                backend, offline
tests/frontend/       browser logic, via node --test
requirements.txt      what the app needs
requirements-dev.txt  plus pytest and httpx2, for the tests
render.yaml           the Render Blueprint
```

## Tests

```bash
pip install -r requirements-dev.txt       # the app's dependencies, pytest and httpx2
python -m pytest tests/ -q                # 140 backend
node --test "tests/frontend/*.test.js"    # 102 browser logic
```

242 tests, no network and no keys, run on every push and pull request. They
cover the parts where being wrong is quiet rather than loud:

- **Chunk splitting** — every character survives, the first chunk stays short,
  nothing exceeds the cap even with no punctuation to cut on.
- **Deepgram turn assembly** — settled segments join into one utterance and
  nothing fires until endpointing does. Acting on `is_final` alone would chop
  sentences into fragments, each triggering its own reply.
- **Barge-in** — an interrupted turn still records what was already generated,
  so the conversation stays coherent; a turn cancelled before any token records
  nothing at all. `interrupted` is sent for a running turn or for audio still
  audibly playing after the turn ended, and once per utterance.
- **Echo** — whole-word matching (so "no" is not found inside "know"), a
  two-word floor, and an expiry so an old reply cannot swallow a new answer.
- **TTS delivery** — a finished chunk ships without waiting for more text, and
  cancelling the consumer cancels every outstanding synthesis.
- **Failure modes** — a TTS failure still releases the UI, an unexpected crash
  does not leak internal detail into a user-facing message.
- **The Murf request shape** — the streaming endpoint is used, and a 400 that
  names the style drops it and retries, per voice and model; any other 400 does
  not.
- **The handshake and HTTP surface** — malformed first frames get typed errors,
  session ids are validated, a foreign Origin is refused, security headers are
  present, static files revalidate, and the history routes never create.
- **Key isolation** — a visitor's key overrides the server's, a blank field
  falls back rather than blanking a working key, and no module anywhere holds
  credentials. That last one is a regression guard: the original build kept a
  single global dict and handed one visitor's keys to the next.
- **Failing loudly** — if the transcription stream drops or the event pump
  crashes, the browser is told. Silence there would leave the UI on "Listening"
  forever with the socket still open.
- **Microphone capture** — the 48k→16k resampler keeps amplitude and loses no
  samples across callbacks, and full-scale input clamps instead of wrapping. A
  wrap here would be an audible click and quietly worse transcription.
- **Playback scheduling** — chunks butt up against each other exactly, and audio
  that finishes decoding *after* the user interrupts is discarded rather than
  speaking over them. `PlaybackGate` leaves "Speaking" only when the server has
  finished sending and the player has run dry, in either order.
- **Start-up helpers** — the retry budget a cold start needs, the wording of a
  microphone failure, feature detection, and session ids the server will keep.

CI also boots the app and hits `/health`, which catches the class of break a unit
test cannot: a bad import, a template that stopped rendering, a route that
disappeared.

## Notes

- Mic capture uses an `AudioWorklet` and is never routed to the speakers, so the
  assistant's voice cannot feed back into the transcriber.
- Conversation history lives in memory and is keyed by the session id in the URL
  — share the link or reload and the conversation continues. It does not survive
  a server restart. An id the server will not accept (8–64 characters of
  letters, digits, `_` and `-`) is replaced rather than trusted.
- `Space` toggles the mic when nothing else is focused.

## Measured

Ten turns end to end against the live APIs on 2026-10-02, ten different
questions, one turn each, on a laptop on a home connection. The clock starts when
the transcript reaches the pipeline and stops when the server sends each frame, so
it leaves out Deepgram's 350 ms endpointing and the browser's decode time.

| Server time from transcript | Median | Range |
|---|---|---|
| First token from the model | 0.62 s | 0.53–1.50 s |
| **First audio frame sent** | **1.28 s** | 0.86–2.48 s |
| Whole reply sent | 1.59 s | 1.20–2.48 s |

The model stayed under 1 s to its first token except once. The slow runs spent
their extra time between the first token and the first audio, which is chunking
plus Murf, not the model.

Time to first audio is the number that matters. It started at 4.0 s:

| Change | Result |
|---|---|
| Cut the first chunk on a clause, not a sentence | 4.0 s → 3.2 s |
| Murf's streaming endpoint instead of `/v1/speech/generate` | 3.2 s → ~1.4 s |
| A faster model, `gemma4:31b` (see below) | ~1.4 s → 1.28 s median (different days, so not like for like) |

The first two were measured on 2026-09-07, before the hardening work; they were
single runs, not a median. The first was a measurement surprise: the persona asks
for one-sentence replies, so a sentence-only rule meant the only boundary was at
the very end and the pipelining never engaged at all. The second is simply a much
faster endpoint. Measured on identical text, `generate` took 2865 ms to return
anything, while `stream` delivers its first byte in 150–280 ms.

The third row is a smaller gain than it looks. What the model switch really
bought is consistency. On the day of the 10-run test, `nemotron-3-nano:30b` took
18–30 s to produce its first token (six runs out of six) and first audio arrived
19–30 s after the question. `gemma4:31b` never went past 1.5 s to its first token.

Not re-measured since 2026-09-07: transcription accuracy (word-perfect on a clean
3.3 s sample).

## Engineering notes

The parts that were interesting to build:

**Pipelined synthesis.** Reply text is cut on clause boundaries as it streams
out of the model, up to three chunks are synthesised concurrently, and results
are yielded strictly in order onto the Web Audio clock. The first chunk's
threshold is deliberately tiny (12 characters) so a short opener ships
immediately — raising it directly increases time-to-first-audio.

**Barge-in as task cancellation.** A turn is one `asyncio.Task`. Interrupting
cancels it, which unwinds the in-flight HTTP request and every pending synthesis
task through normal exception propagation. The partial reply is recorded in a
`finally`, so an interrupted answer still enters history. The browser does not
wait for the server to confirm: it flushes its own queue the moment it sees a
second word.

**What a review found.** Before this version, a review pass (run with AI reviewers)
read the whole codebase and found two bugs the tests had not. Audio was held back until three
chunks were queued or the reply ended, so one- and two-chunk replies waited for
the whole model response. And barge-in did nothing once the model had finished
writing, which is almost always the case, because speech synthesis finishes a few
seconds before the browser has played it out. Both are fixed, and each has a test
that was seen to fail first.

**Measure the model too.** In a live test the agent seemed to go silent: the first
token took 15–30 s, so replies were being talked over, and cancelled, before they
arrived. Murf and the network were fine. The model, `nemotron-3-nano:30b`, had
answered in 207 ms when first measured on 2026-09-07 and was now the slowest. The
fix was to time the candidates on the same key, not to change the code, and
`gemma4:31b` answered in about 0.5 s. `gpt-oss:20b` returned an empty reply and
two others needed paid credits. The default is a constant in `config.py` with the
numbers next to it, so the next person knows to re-measure before changing it.

**No threads.** Deepgram's streaming API is a plain WebSocket, so the entire
backend is single-threaded asyncio. An earlier version used a vendor SDK whose
synchronous `stream()` call forced a worker thread, a blocking queue, and a
thread-safe event bridge; switching providers deleted all three.

**Credentials scoped to a connection.** Keys arrive in the opening frame and
live only on the connection object — frozen, and never assigned to module state.
Verified end to end: with two sockets open at once, a bad key fails its own
session while the other stays connected and working.
