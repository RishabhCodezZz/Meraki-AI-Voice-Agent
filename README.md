# Meraki

A real-time voice agent. You talk, it thinks, it talks back — with audio starting
before the model has finished its sentence.

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
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8000 and add three keys in Setup.

| Service | Does | Free tier |
|---|---|---|
| [Deepgram](https://console.deepgram.com/signup) | speech → text | $200 credit (~690 hours) |
| [Ollama Cloud](https://ollama.com/settings/keys) | the thinking | free tier covers every model below |
| [Murf](https://murf.ai/api/docs/introduction/overview) | text → speech | trial credits |

Keys are held in your browser and sent only to your own WebSocket session. The
server never stores them — two people using the same deployment cannot see or
spend each other's credits.

Prefer server-side keys? Copy `.env.example` to `.env` and fill it in. Anything
entered in the UI takes precedence.

## Models

All six are on Ollama Cloud's free tier. Ordered by how quickly they start
talking, which matters more here than in a chat window — every extra second of
thinking is a second of silence.

| Model | Notes |
|---|---|
| `nemotron-3-nano:30b` | default; fastest to first word |
| `gpt-oss:20b` | fast |
| `gemma4:31b` | balanced |
| `nemotron-3-super` | smarter, slower |
| `gpt-oss:120b` | smarter, slower |
| `nemotron-3-ultra` | slowest |

Nemotron models are reasoning models, so Meraki sends `think: false`. Reasoning
tokens arrive before anything speakable and buy nothing when the output is audio.

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

Covers chunk splitting, the session store, and key resolution. No network, no
keys required.

## Notes

- Mic capture uses an `AudioWorklet` and is never routed to the speakers, so the
  assistant's voice cannot feed back into the transcriber.
- Conversation history lives in memory and is keyed by the session id in the URL
  — share the link or reload and the conversation continues. It does not survive
  a server restart.
- `Space` toggles the mic when nothing else is focused.
