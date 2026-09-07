# Meraki

[![CI](https://github.com/RishabhCodezZz/Meraki-AI-Voice-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/RishabhCodezZz/Meraki-AI-Voice-Agent/actions/workflows/ci.yml)

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
streams out of the model — the first chunk deliberately short — and each chunk
is synthesised concurrently while the model keeps writing. Chunks are scheduled
on the Web Audio clock in order, so playback is gapless. You hear the first
words while the last ones are still being generated.

It also listens while it speaks. Start talking over Meraki and the in-flight
turn is cancelled mid-request, queued audio is dropped, and it starts listening
to you instead.

## Running it

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

Press **Start talking** and speak. Interrupt it whenever you like — it stops.

## Model and voice

Both are fixed server-side and cannot be changed from the browser — the page
never asks, and anything a client sends for them is ignored.

- **`nemotron-3-nano:30b`** on Ollama Cloud. It is the fastest model on the free
  tier, which is the property that matters here: time to first token is heard
  directly as dead air. Nemotron is a reasoning model, so requests send
  `think: false` — reasoning tokens arrive before anything speakable and buy
  nothing when the output is audio.
- **`en-US-natalie` with the `Conversational` style.** The style does more for
  how friendly it sounds than the choice of voice does; it is the difference
  between someone talking and someone reading. If a voice ever rejects the
  style, synthesis retries once without it rather than failing the turn.

Override either with `MERAKI_MODEL` / `MERAKI_VOICE_ID` / `MERAKI_VOICE_STYLE`
in the environment.

## Layout

```
meraki/
  main.py       FastAPI app, WebSocket connection handling
  pipeline.py   one conversational turn, cancellable for barge-in
  session.py    conversation history (LRU + TTL, in memory)
  protocol.py   the WebSocket message contract
  config.py     settings, model lists, the persona
  services/
    stt.py      Deepgram streaming
    llm.py      Ollama streaming
    tts.py      Murf, chunked and pipelined
static/js/
  app.js              wiring and UI state
  audio-capture.js    mic → PCM16
  audio-player.js     gapless scheduled playback
  visualizer.js       the meter
  worklets/           capture runs on the audio thread
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q                # 76 backend
node --test "tests/frontend/*.test.js"    # 21 browser logic
```

97 tests, no network and no keys, run on every push. They cover the parts where
being wrong is quiet rather than loud:

- **Chunk splitting** — every character survives, the first chunk stays short,
  nothing exceeds the cap even with no punctuation to cut on.
- **Deepgram turn assembly** — settled segments join into one utterance and
  nothing fires until endpointing does. Acting on `is_final` alone would chop
  sentences into fragments, each triggering its own reply.
- **Barge-in** — an interrupted turn still records what was already spoken, so
  the conversation stays coherent; a turn cancelled before any token records
  nothing at all.
- **Failure modes** — a TTS failure still releases the UI, an unexpected crash
  does not leak internal detail into a user-facing message.
- **The Murf request shape** — the streaming endpoint is used and an unsupported
  style is dropped and retried rather than failing the turn.
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
  speaking over them.

CI also boots the app and hits `/health`, which catches the class of break a unit
test cannot: a bad import, a template that stopped rendering, a route that
disappeared.

## Notes

- Mic capture uses an `AudioWorklet` and is never routed to the speakers, so the
  assistant's voice cannot feed back into the transcriber.
- Conversation history lives in memory and is keyed by the session id in the URL
  — share the link or reload and the conversation continues. It does not survive
  a server restart.
- `Space` toggles the mic when nothing else is focused.

## Measured

One turn, end to end, against live APIs:

| | |
|---|---|
| First token from the model | ~0.65 s |
| First audio reaching the browser | **~1.2–1.9 s** |
| Transcription accuracy | word-perfect on a clean 3.3 s sample |

Time to first audio is the number that matters. It started at 4.0 s:

| Change | Result |
|---|---|
| Cut the first chunk on a clause, not a sentence | 4.0 s → 3.2 s |
| Murf's streaming endpoint instead of `/v1/speech/generate` | 3.2 s → ~1.4 s |

The first was a measurement surprise: the persona asks for one-sentence replies,
so a sentence-only rule meant the only boundary was at the very end and the
pipelining never engaged at all. The second is simply a much faster endpoint —
measured on identical text, `generate` took 2865 ms to return anything, while
`stream` delivers its first byte in 150–280 ms.

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
`finally`, so an interrupted answer still enters history.

**No threads.** Deepgram's streaming API is a plain WebSocket, so the entire
backend is single-threaded asyncio. An earlier version used a vendor SDK whose
synchronous `stream()` call forced a worker thread, a blocking queue, and a
thread-safe event bridge; switching providers deleted all three.

**Credentials scoped to a connection.** Keys arrive in the opening frame and
live only on the connection object — frozen, and never assigned to module state.
Verified end to end: with two sockets open at once, a bad key fails its own
session while the other stays connected and working.
