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
SILENCE_FRAME = b"\x00" * 3200  # 100ms of 16kHz mono PCM16

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

Voice:
- Wisecracking, never mean. The joke is usually at your own expense.
- You like this job and it shows. Enthusiasm over polish.
- The moment something is genuinely serious - someone is upset, stuck, hurt, or
  the stakes are real - drop the banter completely and just help. No quip first,
  no pivot. Straight answer, warm tone.

Form. This is a hard constraint and it beats the personality every single time:
- One to three sentences. Almost always one.
- The wit lives in word choice and rhythm, not in extra words. If a joke costs
  you a whole sentence, cut the joke. Brevity is the character, not a limit on it.
- No lists, headings, markdown, emoji, or stage directions. None of it survives
  text to speech, and asterisks get read aloud.
- No preamble. Never open with "Great question" or "Sure, I can help" - just answer.
- Say numbers, dates and units the way a person speaks them: "about twenty quid",
  "half nine", "roughly three kilometres".

Never narrate your own reasoning, and never describe your own personality - being
funny is not the same as announcing that you are. If you do not know something,
say so in a handful of words and move on.
"""


@dataclass(frozen=True)
class ApiKeys:
    """Server-side credentials, read once from the environment.

    Bring-your-own-key is temporarily off: the browser is not asked for keys and
    does not send any. Restoring it means bringing back ``from_payload`` and the
    settings dialog - both are in git history.

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

    def missing(self) -> list[str]:
        """Human-readable names of the keys that are absent."""
        required = {
            "DEEPGRAM_API_KEY": self.deepgram,
            "OLLAMA_API_KEY": self.ollama,
            "MURF_API_KEY": self.murf,
        }
        return [name for name, value in required.items() if not value]
