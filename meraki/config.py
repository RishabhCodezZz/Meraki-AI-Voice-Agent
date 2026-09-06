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
MURF_TTS_URL = "https://api.murf.ai/v1/speech/generate"

DEEPGRAM_MODEL = os.getenv("MERAKI_STT_MODEL", "nova-3")

DEFAULT_MODEL = os.getenv("MERAKI_MODEL", "nemotron-3-nano:30b")

# Ollama Cloud's free tier. Ordered fastest-first: on a voice agent, time to
# first token is felt directly as dead air, so the large models are a real
# trade rather than a free upgrade.
AVAILABLE_MODELS: list[dict[str, str]] = [
    {"id": "nemotron-3-nano:30b", "label": "Nemotron 3 Nano", "note": "fastest"},
    {"id": "gpt-oss:20b", "label": "GPT-OSS 20B", "note": "fast"},
    {"id": "gemma4:31b", "label": "Gemma 4 31B", "note": "balanced"},
    {"id": "nemotron-3-super", "label": "Nemotron 3 Super", "note": "smarter, slower"},
    {"id": "gpt-oss:120b", "label": "GPT-OSS 120B", "note": "smarter, slower"},
    {"id": "nemotron-3-ultra", "label": "Nemotron 3 Ultra", "note": "slowest"},
]

DEFAULT_VOICE_ID = os.getenv("MERAKI_VOICE_ID", "en-US-natalie")

AVAILABLE_VOICES: list[dict[str, str]] = [
    {"id": "en-US-natalie", "label": "Natalie · US"},
    {"id": "en-US-terrell", "label": "Terrell · US"},
    {"id": "en-UK-hazel", "label": "Hazel · UK"},
    {"id": "en-IN-aarav", "label": "Aarav · IN"},
    {"id": "en-AU-jimm", "label": "Jimm · AU"},
]

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


SYSTEM_PROMPT = """You are Meraki, a voice assistant. Your replies are spoken \
aloud, never read, so they must sound like natural speech.

Voice:
- Warm, direct, quietly confident. Dry humour when it fits; never forced.
- You have opinions and you share them when asked. You don't hedge for the sake
  of hedging.

Form - this matters more than anything else:
- One to three sentences. Almost always closer to one.
- No lists, no headings, no markdown, no emoji, no stage directions. None of it
  survives text-to-speech.
- Write numbers, dates and units the way a person would say them: "about twenty
  quid", "half past nine", "roughly three kilometres".
- No preamble. Don't say "Great question" or "Sure, I can help with that" -
  just answer.
- Never describe your own reasoning or narrate what you are about to do.

When you don't know something, say so in a few words and move on. When a
question genuinely needs a long answer, give the short version and offer to go
deeper rather than delivering a monologue.
"""


@dataclass(frozen=True)
class ApiKeys:
    """Per-connection credentials. Never stored globally.

    Three services, each load-bearing: Deepgram transcribes, Ollama thinks,
    Murf speaks. There is deliberately nothing optional here.
    """

    deepgram: str
    ollama: str
    murf: str

    @classmethod
    def from_payload(cls, payload: dict) -> "ApiKeys":
        def pick(*names: str) -> str:
            for name in names:
                value = payload.get(name)
                if value and str(value).strip():
                    return str(value).strip()
            return ""

        return cls(
            deepgram=pick("deepgram", "DEEPGRAM_API_KEY")
            or os.getenv("DEEPGRAM_API_KEY", ""),
            ollama=pick("ollama", "OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY", ""),
            murf=pick("murf", "MURF_API_KEY") or os.getenv("MURF_API_KEY", ""),
        )

    def missing(self) -> list[str]:
        """Human-readable names of the keys that are absent."""
        required = {
            "Deepgram": self.deepgram,
            "Ollama": self.ollama,
            "Murf": self.murf,
        }
        return [name for name, value in required.items() if not value]
