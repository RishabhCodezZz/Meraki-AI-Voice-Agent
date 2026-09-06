"""Murf text-to-speech, pipelined for low time-to-first-audio.

The old implementation waited for the entire model reply, synthesised it in one
request, downloaded the whole MP3, then sent it. Nothing was audible until every
stage had finished.

Here the reply is split into clause-sized chunks as it streams in. The first
chunk is deliberately short so sound starts almost immediately; later chunks are
longer to keep request count down. Synthesis runs concurrently but results are
yielded strictly in order, so playback is seamless.

Two things keep each chunk cheap:
  - ``encodeAsBase64`` returns the audio in the response body, so there is no
    second round trip to download the MP3 from a URL.
  - 24 kHz instead of Murf's 44.1 kHz default is roughly half the bytes, and
    speech does not need the headroom.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import AsyncGenerator, AsyncIterable, Optional

import aiohttp

from ..config import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    FIRST_CHUNK_MIN_CHARS,
    MURF_TTS_URL,
    TTS_SAMPLE_RATE,
    TTS_TIMEOUT,
    VOICE_ID,
    VOICE_STYLE,
)

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 3

# A boundary we are happy to cut on, strongest first.
_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—]\s")

# Set once, the first time Murf tells us the style is not valid for this voice,
# so we stop paying for a failed request on every subsequent chunk.
_style_supported = True


class TTSError(RuntimeError):
    pass


async def chunk_stream(text_stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    """Regroup a token stream into speakable chunks."""
    buffer = ""
    is_first = True

    async for token in text_stream:
        buffer += token
        while True:
            threshold = FIRST_CHUNK_MIN_CHARS if is_first else CHUNK_MIN_CHARS
            cut = _find_cut(buffer, threshold, allow_clause=is_first)
            if cut is None:
                break
            chunk, buffer = buffer[:cut].strip(), buffer[cut:]
            if chunk:
                is_first = False
                yield chunk

    tail = buffer.strip()
    if tail:
        yield tail


def _find_cut(
    buffer: str, threshold: int, *, allow_clause: bool = False
) -> Optional[int]:
    """Index to split ``buffer`` at, or None to keep accumulating."""
    if len(buffer) < threshold:
        return None

    window = buffer[:CHUNK_MAX_CHARS]

    for match in _SENTENCE_END.finditer(window):
        if match.end() >= threshold:
            return match.end()

    if allow_clause:
        # Only the first chunk cuts this eagerly. Most replies here are a single
        # sentence, so waiting for a full stop means waiting for the whole reply
        # and pipelining buys nothing - the comma is what gets audio started.
        for match in _CLAUSE_END.finditer(window):
            if match.end() >= threshold:
                return match.end()

    if len(buffer) >= CHUNK_MAX_CHARS:
        for match in _CLAUSE_END.finditer(window):
            if match.end() >= threshold:
                return match.end()
        # Nothing punctuated in range - fall back to the last word break.
        space = window.rfind(" ")
        if space > threshold:
            return space + 1
        return CHUNK_MAX_CHARS

    return None


async def synthesize(
    session: aiohttp.ClientSession,
    api_key: str,
    text: str,
    voice_id: str = VOICE_ID,
) -> str:
    """Render one chunk of text and return it as base64 MP3."""
    if not api_key:
        raise TTSError("Murf API key is missing.")

    global _style_supported

    plain = {
        "text": text,
        "voiceId": voice_id,
        "format": "MP3",
        "sampleRate": TTS_SAMPLE_RATE,
        "encodeAsBase64": True,
    }
    styled = VOICE_STYLE and _style_supported
    # Build a separate dict rather than mutating one across both attempts.
    payload = {**plain, "style": VOICE_STYLE} if styled else plain

    result, rejected = await _post(session, api_key, payload)

    if rejected is not None and styled:
        # This voice does not take the configured style. Drop it and carry on
        # rather than failing the turn; remember so we stop retrying.
        logger.warning(
            "Murf rejected style %r for %s; continuing without it (%s)",
            VOICE_STYLE,
            voice_id,
            rejected,
        )
        _style_supported = False
        result, rejected = await _post(session, api_key, plain)

    if result is None:
        raise TTSError(f"Murf rejected the request: {rejected}")

    if encoded := result.get("encodedAudio"):
        return encoded

    # Older accounts may still answer with a URL instead.
    audio_url = result.get("audioFile")
    if not audio_url:
        raise TTSError("Murf returned no audio.")

    timeout = aiohttp.ClientTimeout(total=TTS_TIMEOUT)
    async with session.get(audio_url, timeout=timeout) as response:
        if response.status != 200:
            raise TTSError("Could not download the generated audio.")
        return base64.b64encode(await response.read()).decode("ascii")


async def _post(
    session: aiohttp.ClientSession, api_key: str, payload: dict
) -> tuple[Optional[dict], Optional[str]]:
    """Returns (result, None) on success, or (None, detail) on a 400."""
    timeout = aiohttp.ClientTimeout(total=TTS_TIMEOUT)
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    async with session.post(
        MURF_TTS_URL, json=payload, headers=headers, timeout=timeout
    ) as response:
        if response.status == 400:
            return None, (await response.text())[:200]
        if response.status != 200:
            detail = (await response.text())[:200]
            logger.error("Murf %s: %s", response.status, detail)
            raise TTSError(_explain(response.status))
        return await response.json(), None


async def stream_speech(
    session: aiohttp.ClientSession,
    api_key: str,
    text_stream: AsyncIterable[str],
    voice_id: str = VOICE_ID,
) -> AsyncGenerator[str, None]:
    """Yield base64 MP3 chunks, in order, as the text arrives.

    Up to ``MAX_IN_FLIGHT`` chunks are synthesised concurrently. Cancelling the
    consumer cancels any outstanding synthesis.
    """
    pending: list[asyncio.Task[str]] = []

    async def drain(limit: int) -> AsyncGenerator[str, None]:
        while len(pending) > limit:
            yield await pending.pop(0)

    try:
        async for chunk in chunk_stream(text_stream):
            pending.append(
                asyncio.create_task(synthesize(session, api_key, chunk, voice_id))
            )
            async for ready in drain(MAX_IN_FLIGHT - 1):
                yield ready

        async for ready in drain(0):
            yield ready
    finally:
        for task in pending:
            task.cancel()


def _explain(status: int) -> str:
    if status in (401, 403):
        return "Murf rejected that API key."
    if status == 429:
        return "Murf rate limit reached."
    return f"Murf returned {status}."
