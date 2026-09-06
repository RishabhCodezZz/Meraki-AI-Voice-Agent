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
