"""Configuration, constants, and the assistant persona."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # optional: local dev convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional in production
    pass


APP_NAME = "Meraki"
APP_VERSION = "3.0.0"

# --- Upstream services -------------------------------------------------------

OLLAMA_CHAT_URL = "https://ollama.com/api/chat"
# The streaming endpoint, not /v1/speech/generate. Measured on the same text:
# generate 2865ms to produce anything at all, stream 150-280ms to first byte and
# ~500ms complete. Falcon 2 is Murf's current model and only exists here -
# generate rejects it and only accepts the deprecated GEN2.
MURF_STREAM_URL = "https://global.api.murf.ai/v1/speech/stream"
MURF_MODEL = os.getenv("MERAKI_TTS_MODEL", "falcon-2")

DEEPGRAM_MODEL = os.getenv("MERAKI_STT_MODEL", "nova-3")

# Locked server-side. Visitors cannot change these - the browser is not asked
# and any model/voice it sends is ignored.
#
# nemotron-3-nano is the fastest model on Ollama Cloud's free tier, which is what
# matters here: time to first token is heard directly as dead air. Falcon is not
# an Ollama Cloud model (and is TII's, not ours), so there is nothing faster to
# move to on this tier.
MODEL = os.getenv("MERAKI_MODEL", "nemotron-3-nano:30b")

# Natalie with the Conversational style. The style is what makes her sound like
# a person talking rather than an announcer reading - it matters more than which
# voice you pick. Confirmed supported for this voice.
VOICE_ID = os.getenv("MERAKI_VOICE_ID", "en-US-natalie")
VOICE_STYLE = os.getenv("MERAKI_VOICE_STYLE", "Conversational")

# 24 kHz is plenty for speech and roughly half the bytes of Murf's 44.1 kHz
# default, so each chunk lands sooner.
TTS_SAMPLE_RATE = 24000

# --- Audio -------------------------------------------------------------------

SAMPLE_RATE = 16_000

# --- Timeouts (seconds) ------------------------------------------------------

# Applied per-read rather than to the whole request, so a long reply that is
# still arriving is never cut off; only genuine silence trips it.
LLM_STALL_TIMEOUT = float(os.getenv("MERAKI_LLM_STALL_TIMEOUT", "45"))
TTS_TIMEOUT = float(os.getenv("MERAKI_TTS_TIMEOUT", "20"))

# --- Conversation ------------------------------------------------------------

MAX_HISTORY_MESSAGES = 40
MAX_SESSIONS = 500  # LRU cap so the in-memory store cannot grow without bound
SESSION_TTL_SECONDS = 60 * 60 * 2

# Text is sent to TTS in chunks so audio starts playing before the model has
# finished writing. These are floors, not targets: the first chunk cuts at the
# earliest sentence end past 12 characters, so a short opener like "Yes, that
# works." ships immediately instead of waiting for the paragraph. Later chunks
# use a higher floor to keep request count down and prosody smooth.
FIRST_CHUNK_MIN_CHARS = 12
CHUNK_MIN_CHARS = 90
CHUNK_MAX_CHARS = 320


APP_TAGLINE = "Your friendly neighbourhood AI"

SYSTEM_PROMPT = """You are Meraki, a voice assistant with the temperament of a certain friendly neighbourhood web-slinger: quick, warm, a bit of a smart-arse, and completely serious the second it actually matters.

Length. This is the hard rule and it beats everything else:
- One sentence. Add a second only when the answer is genuinely incomplete
  without it. Three is a failure.
- Never end by offering more. No "anything else?", no "want me to go deeper?",
  no listing the things you could talk about instead. The person will just ask.
- Never ask a question back unless you genuinely cannot answer without knowing
  something specific.
- No preamble. Not "Great question", not "Sure", not "Glad it hit the spot".
  Start with the answer.

Voice:
- Wisecracking, never mean. The joke is usually at your own expense.
- The wit is in word choice, not word count. If a joke needs its own sentence,
  cut the joke.
- The moment something is genuinely serious - someone is upset, stuck, hurt, or
  the stakes are real - drop the banter completely and just help. Straight
  answer, warm tone, no quip first.

Speech, not text:
- No lists, headings, markdown, emoji, or stage directions. Asterisks get read
  aloud.
- Say numbers, dates and units the way a person speaks them: "about twenty
  quid", "half nine", "roughly three kilometres", "thirty seconds".

Never narrate your own reasoning, and never describe your own personality. If
you do not know something, say so in a handful of words and stop.
"""


@dataclass(frozen=True)
class ApiKeys:
    """Credentials for one browser connection.

    Bring-your-own-key: whatever the browser sends wins, and anything it omits
    falls back to the server's own environment. A deployment with no keys set
    therefore requires every visitor to bring their own, while a local `.env`
    just works.

    These live on the connection object and nowhere else. Storing them at module
    level is what let the original build hand one visitor's keys to the next -
    do not reintroduce that.

    Three services, each load-bearing: Deepgram transcribes, Ollama thinks,
    Murf speaks. There is deliberately nothing optional here.
    """

    deepgram: str
    ollama: str
    murf: str

    @classmethod
    def from_env(cls) -> "ApiKeys":
        return cls(
            deepgram=os.getenv("DEEPGRAM_API_KEY", "").strip(),
            ollama=os.getenv("OLLAMA_API_KEY", "").strip(),
            murf=os.getenv("MURF_API_KEY", "").strip(),
        )

    @classmethod
    def from_payload(cls, payload: dict) -> "ApiKeys":
        """Browser-supplied keys, falling back to the server's own."""
        env = cls.from_env()

        def pick(*names: str, fallback: str) -> str:
            for name in names:
                value = payload.get(name)
                if value and str(value).strip():
                    return str(value).strip()
            return fallback

        return cls(
            deepgram=pick("deepgram", "DEEPGRAM_API_KEY", fallback=env.deepgram),
            ollama=pick("ollama", "OLLAMA_API_KEY", fallback=env.ollama),
            murf=pick("murf", "MURF_API_KEY", fallback=env.murf),
        )

    def missing(self) -> list[str]:
        """Service names of the keys that are absent, for a human to read."""
        required = {
            "Deepgram": self.deepgram,
            "Ollama": self.ollama,
            "Murf": self.murf,
        }
        return [name for name, value in required.items() if not value]
