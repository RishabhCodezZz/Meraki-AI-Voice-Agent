"""Murf text-to-speech, pipelined for low time-to-first-audio.

The old implementation waited for the entire model reply, synthesised it in one
request, downloaded the whole MP3, then sent it. Nothing was audible until every
stage had finished.

Here the reply is split into clause-sized chunks as it streams in. The first
chunk is deliberately short so sound starts almost immediately; later chunks are
longer to keep request count down. Synthesis runs concurrently but results are
yielded strictly in order, so playback is seamless.

Synthesis goes to Murf's streaming endpoint rather than ``/v1/speech/generate``.
Measured on identical text, generate took 2865ms to return anything; the stream
endpoint delivers its first byte in 150-280ms and finishes in about 500ms. It is
also the only place Murf's current Falcon 2 model is available - generate rejects
it and accepts only the deprecated GEN2.

24 kHz rather than the 44.1 kHz default roughly halves the bytes, and speech does
not need the headroom.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from contextlib import aclosing
from typing import AsyncGenerator, AsyncIterable, Optional

import aiohttp

from ..config import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    FIRST_CHUNK_MIN_CHARS,
    MURF_MODEL,
    MURF_STREAM_URL,
    TTS_SAMPLE_RATE,
    TTS_TIMEOUT,
    VOICE_ID,
    VOICE_STYLE,
)

logger = logging.getLogger(__name__)

MAX_IN_FLIGHT = 3

# A boundary we are happy to cut on, strongest first.
_SENTENCE_END = re.compile(r"[.!?…]['\")\]]*\s")
# Dashes are commonly typed tight against the next word ("pan-about"), so they
# do not require trailing space; commas and colons do, to avoid cutting inside
# decimals and times.
_CLAUSE_END = re.compile(r"[,;:]\s|[—–]\s?")

# (voice, model) pairs Murf has said do not take the configured style, so we stop
# paying for a failed request on every subsequent chunk. Keyed rather than a
# single flag because one voice refusing a style says nothing about another.
_style_rejected: set[tuple[str, str]] = set()


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

    plain = {
        "text": text,
        "voiceId": voice_id,
        "model": MURF_MODEL,
        "format": "MP3",
        "sampleRate": TTS_SAMPLE_RATE,
        "channelType": "MONO",
    }
    styled = bool(VOICE_STYLE) and (voice_id, MURF_MODEL) not in _style_rejected
    # Build a separate dict rather than mutating one across both attempts.
    payload = {**plain, "style": VOICE_STYLE} if styled else plain

    result, rejected = await _post(session, api_key, payload)

    # Only a 400 that names the style is a style problem. Anything else (credits,
    # a bad voice id) would fail the plain request too, and must not be mistaken
    # for a reason to strip the style from every later call.
    if rejected is not None and styled and "style" in rejected.lower():
        # This voice does not take the configured style. Drop it and carry on
        # rather than failing the turn; remember so we stop retrying.
        logger.warning(
            "Murf rejected style %r for %s; continuing without it (%s)",
            VOICE_STYLE,
            voice_id,
            rejected,
        )
        _style_rejected.add((voice_id, MURF_MODEL))
        result, rejected = await _post(session, api_key, plain)

    if result is None:
        raise TTSError(f"Murf rejected the request: {rejected}")

    return base64.b64encode(result).decode("ascii")


async def _post(
    session: aiohttp.ClientSession, api_key: str, payload: dict
) -> tuple[Optional[bytes], Optional[str]]:
    """Returns (audio bytes, None) on success, or (None, detail) on a 400.

    The endpoint answers with a chunked audio stream, so the body is read as it
    arrives rather than waiting for a JSON envelope.
    """
    timeout = aiohttp.ClientTimeout(total=TTS_TIMEOUT)
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    async with session.post(
        MURF_STREAM_URL, json=payload, headers=headers, timeout=timeout
    ) as response:
        if response.status == 400:
            return None, (await response.text())[:200]
        if response.status != 200:
            detail = (await response.text())[:200]
            logger.error("Murf %s: %s", response.status, detail)
            raise TTSError(_explain(response.status))

        audio = bytearray()
        async for part in response.content.iter_any():
            audio += part
        if not audio:
            raise TTSError("Murf returned no audio.")
        return bytes(audio), None


async def stream_speech(
    session: aiohttp.ClientSession,
    api_key: str,
    text_stream: AsyncIterable[str],
    voice_id: str = VOICE_ID,
) -> AsyncGenerator[str, None]:
    """Yield base64 MP3 chunks, in order, as the text arrives.

    Up to ``MAX_IN_FLIGHT`` chunks are synthesised concurrently. A finished chunk
    is yielded as soon as it and every earlier one are done, without waiting for
    more text. Cancelling the consumer cancels outstanding synthesis and closes
    the text stream.
    """
    slots = asyncio.Semaphore(MAX_IN_FLIGHT)
    # Synth tasks in chunk order, then the end marker or a text-side exception.
    queue: asyncio.Queue = asyncio.Queue()
    tasks: list[asyncio.Task[str]] = []
    done = object()

    async def produce() -> None:
        # The producer runs on its own so the text stream keeps being pulled
        # while the consumer is waiting on a synth task; otherwise text frames
        # stall behind audio.
        text_iter = text_stream.__aiter__()
        try:
            async with aclosing(chunk_stream(text_iter)) as chunks:
                async for chunk in chunks:
                    await slots.acquire()
                    task = asyncio.create_task(
                        synthesize(session, api_key, chunk, voice_id)
                    )
                    task.add_done_callback(lambda _: slots.release())
                    tasks.append(task)
                    queue.put_nowait(task)
            queue.put_nowait(done)
        except Exception as exc:
            queue.put_nowait(exc)
        finally:
            # chunk_stream closing does not close the stream it reads from.
            aclose = getattr(text_iter, "aclose", None)
            if aclose is not None:
                await aclose()

    producer = asyncio.create_task(produce())
    try:
        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, Exception):
                raise item
            yield await item
    finally:
        producer.cancel()
        for task in tasks:
            task.cancel()
        # Await them so a failed task is retrieved, not left to log
        # "exception was never retrieved" when it is garbage collected.
        await asyncio.gather(producer, *tasks, return_exceptions=True)


def _explain(status: int) -> str:
    if status in (401, 403):
        return "Murf rejected that API key."
    if status == 429:
        return "Murf rate limit reached."
    return f"Murf returned {status}."
