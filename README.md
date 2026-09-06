# Meraki

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

Copy `.env.example` to `.env` and fill in three values:

```
DEEPGRAM_API_KEY=
OLLAMA_API_KEY=
MURF_API_KEY=
```

| Key | From | Free tier |
|---|---|---|
| Deepgram | [console.deepgram.com/signup](https://console.deepgram.com/signup) | $200 credit, roughly 690 hours |
| Ollama | [ollama.com/settings/keys](https://ollama.com/settings/keys) | covers the model below |
| Murf | [murf.ai](https://murf.ai/api/docs/introduction/overview) | trial credits |

Keys are read from the environment at startup. The browser is never asked for
them and never sends any — anything a client puts in the handshake is ignored.

Note that this means whoever opens the page spends *your* credits, which is the
right trade while developing and the wrong one for a public link. Bring-your-own
key is in git history and can come back before this is hosted again.

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
python -m pytest tests/ -q
```

46 tests, no network and no keys. They cover the parts where being wrong is
quiet rather than loud:

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
- **The Murf request shape** — inline base64 is requested (no download round
  trip) and an unsupported style is dropped and retried rather than failing.

## Notes

- Mic capture uses an `AudioWorklet` and is never routed to the speakers, so the
  assistant's voice cannot feed back into the transcriber.
- Conversation history lives in memory and is keyed by the session id in the URL
  — share the link or reload and the conversation continues. It does not survive
  a server restart.
- `Space` toggles the mic when nothing else is focused.

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
live only on the connection object. There is no module-level key state, so
concurrent visitors cannot see or spend each other's credits.
