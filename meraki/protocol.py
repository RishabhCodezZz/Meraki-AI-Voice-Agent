"""WebSocket message contract between the browser and the server.

Every frame is JSON with a ``type`` discriminator. Client frames are documented
here too so the two halves stay in sync.

Client -> server
    {"type": "config", "session_id": str, "keys": {...}}   first frame, JSON
    "stop"                            plain text, not JSON: user released the mic
    <binary>                          PCM16 mono @16kHz

The handshake is validated, and every failure is a typed fatal ``error`` frame
followed by a close - never a traceback: a first frame that is binary, not JSON,
not an object, not ``config``, or late (15s) is code ``handshake``; no usable
keys is code ``keys``, and so is a Deepgram key that Deepgram refused (401/403) -
the browser answers ``keys`` with "Open Keys" rather than "Retry". Any other
Deepgram failure at connect time is ``stt``. ``keys`` that is not an object counts as no keys.
``session_id`` is kept only if it matches ``[A-Za-z0-9_-]{8,64}`` (so a reload
resumes the conversation); anything else, or nothing, gets a fresh server-side
id. There is no shared fallback id. The upgrade itself is refused (close 1008)
when the browser's Origin is not this host or in ``MERAKI_ALLOWED_ORIGINS``.

Model and voice are server-side settings; anything a client sends for them is
ignored. Any key it omits falls back to the server's environment.

Server -> client
    {"type": "ready"}                 STT connected, safe to send audio
    {"type": "partial",  "text": str}
    {"type": "final",    "text": str}
    {"type": "thinking"}
    {"type": "reply_chunk", "text": str}
    {"type": "reply_done",  "text": str}
    {"type": "audio", "seq": int, "data": <base64 mp3>}
    {"type": "speech_done"}
    {"type": "interrupted"}           user barged in; drop queued audio. Also sent
                                      for a turn that has already finished while
                                      its audio is still playing in the browser
    {"type": "error", "code": str, "message": str, "fatal": bool}
"""

from __future__ import annotations

from typing import Any


def ready() -> dict[str, Any]:
    return {"type": "ready"}


def partial(text: str) -> dict[str, Any]:
    return {"type": "partial", "text": text}


def final(text: str) -> dict[str, Any]:
    return {"type": "final", "text": text}


def thinking() -> dict[str, Any]:
    return {"type": "thinking"}


def reply_chunk(text: str) -> dict[str, Any]:
    return {"type": "reply_chunk", "text": text}


def reply_done(text: str) -> dict[str, Any]:
    return {"type": "reply_done", "text": text}


def audio(seq: int, data: str) -> dict[str, Any]:
    return {"type": "audio", "seq": seq, "data": data}


def speech_done() -> dict[str, Any]:
    return {"type": "speech_done"}


def interrupted() -> dict[str, Any]:
    return {"type": "interrupted"}


def error(code: str, message: str, *, fatal: bool = False) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message, "fatal": fatal}
