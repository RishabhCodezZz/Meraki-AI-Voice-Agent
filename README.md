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
pip install -r requirements.txt
python run.py
```

Then open **http://127.0.0.1:8000**.

### Where the keys go

Click **Settings** (top right) → three password fields → **Save**. That is the
only place you need to paste anything. The dialog opens by itself on first load.

| Field | Get it from | Free tier |
|---|---|---|
| Deepgram | [console.deepgram.com/signup](https://console.deepgram.com/signup) | $200 credit, roughly 690 hours |
| Ollama | [ollama.com/settings/keys](https://ollama.com/settings/keys) | covers every model listed below |
| Murf | [murf.ai](https://murf.ai/api/docs/introduction/overview) | trial credits |

Keys are stored in your browser's `localStorage` and travel only to your own
WebSocket session. The server holds them on the connection object and nowhere
else, so two people on the same deployment can never see or spend each other's
credits.

Press **Start talking** and speak. Interrupt it whenever you like — it stops.

Prefer server-side keys? Copy `.env.example` to `.env` and fill it in; anything
entered in the UI wins over the file.

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
