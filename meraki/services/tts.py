"""Murf text-to-speech, pipelined for low time-to-first-audio.

The old implementation waited for the entire model reply, synthesised it in one
request, downloaded the whole MP3, then sent it. Nothing was audible until every
stage had finished.

Here the reply is split into clause-sized chunks as it streams in. The first
chunk is deliberately short so sound starts almost immediately; later chunks are
longer to keep request count down. Synthesis runs concurrently but results are
yielded strictly in order, so playback is seamless.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import AsyncGenerator, AsyncIterable

import aiohttp

from ..config import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    FIRST_CHUNK_MIN_CHARS,
    MURF_TTS_URL,
    TTS_TIMEOUT,
)

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 3

# A boundary we are happy to cut on, strongest first.
_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—]\s")


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
            cut = _find_cut(buffer, threshold)
            if cut is None:
                break
            chunk, buffer = buffer[:cut].strip(), buffer[cut:]
            if chunk:
                is_first = False
                yield chunk

    tail = buffer.strip()
    if tail:
        yield tail


def _find_cut(buffer: str, threshold: int) -> int | None:
    """Index to split ``buffer`` at, or None to keep accumulating."""
    if len(buffer) < threshold:
        return None

    window = buffer[:CHUNK_MAX_CHARS]

    last_sentence = None
    for match in _SENTENCE_END.finditer(window):
        if match.end() >= threshold:
            last_sentence = match.end()
            break
    if last_sentence:
        return last_sentence

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
    voice_id: str,
) -> str:
    """Render one chunk of text and return it as base64 MP3."""
    if not api_key:
        raise TTSError("Murf API key is missing.")

    timeout = aiohttp.ClientTimeout(total=TTS_TIMEOUT)
    payload = {"text": text, "voiceId": voice_id, "format": "MP3"}
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    async with session.post(
        MURF_TTS_URL, json=payload, headers=headers, timeout=timeout
    ) as response:
        if response.status != 200:
            detail = (await response.text())[:300]
            logger.error("Murf %s: %s", response.status, detail)
            raise TTSError(_explain(response.status))
        result = await response.json()

    audio_url = result.get("audioFile")
    if not audio_url:
        raise TTSError("Murf returned no audio.")

    async with session.get(audio_url, timeout=timeout) as audio_response:
        if audio_response.status != 200:
            raise TTSError("Could not download the generated audio.")
        blob = await audio_response.read()

    return base64.b64encode(blob).decode("ascii")


async def stream_speech(
    session: aiohttp.ClientSession,
    api_key: str,
    text_stream: AsyncIterable[str],
    voice_id: str,
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
