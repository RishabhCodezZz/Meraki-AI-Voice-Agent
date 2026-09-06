"""WebSocket message contract between the browser and the server.

Every frame is JSON with a ``type`` discriminator. Client frames are documented
here too so the two halves stay in sync.

Client -> server
    {"type": "config", "session_id": str, "keys": {...}, "voice_id": str}
    {"type": "stop"}                  user released the mic
    <binary>                          PCM16 mono @16kHz

Server -> client
    {"type": "ready"}                 STT connected, safe to send audio
    {"type": "partial",  "text": str}
    {"type": "final",    "text": str}
    {"type": "thinking"}
    {"type": "reply_chunk", "text": str}
    {"type": "reply_done",  "text": str}
    {"type": "audio", "seq": int, "data": <base64 mp3>}
    {"type": "speech_done"}
    {"type": "interrupted"}           user barged in; drop queued audio
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
