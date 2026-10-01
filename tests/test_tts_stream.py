"""Delivery order and timing of ``stream_speech``.

``synthesize`` is replaced with a fake that sleeps, so these assert when audio
becomes available rather than what Murf returns.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from meraki.services import tts


def run(coro):
    return asyncio.run(coro)


async def _chunks(*items: str):
    for item in items:
        yield item


def _fake_chunks(monkeypatch, *items: str):
    """Bypass real chunking so each test controls exactly what the chunks are."""
    monkeypatch.setattr(tts, "chunk_stream", lambda _stream: _chunks(*items))


async def _no_text():
    return
    yield  # pragma: no cover - makes this an async generator


def test_first_audio_is_yielded_before_the_text_stream_ends(monkeypatch):
    """A one-chunk reply must not wait for the LLM to finish before sounding."""

    async def fake_synth(session, api_key, text, voice_id):
        await asyncio.sleep(0.05)
        return f"audio:{text}"

    monkeypatch.setattr(tts, "synthesize", fake_synth)

    async def text():
        yield "Hello there, friend. "
        await asyncio.sleep(0.6)
        yield "More text here."

    async def go():
        start = time.monotonic()
        first_at = None
        out = []
        async for audio in tts.stream_speech(None, "key", text()):
            if first_at is None:
                first_at = time.monotonic() - start
            out.append(audio)
        return first_at, out

    first_at, out = run(go())

    assert first_at < 0.4
    assert len(out) == 2


def test_chunks_are_yielded_in_order_even_if_a_later_one_finishes_first(monkeypatch):
    delays = {"slow": 0.2, "fast": 0.01}

    async def fake_synth(session, api_key, text, voice_id):
        await asyncio.sleep(delays[text])
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    _fake_chunks(monkeypatch, "slow", "fast")

    async def go():
        return [a async for a in tts.stream_speech(None, "key", _no_text())]

    assert run(go()) == ["slow", "fast"]


def test_at_most_max_in_flight_synth_calls_run_at_once(monkeypatch):
    running = 0
    peak = 0

    async def fake_synth(session, api_key, text, voice_id):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.03)
        finally:
            running -= 1
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    _fake_chunks(monkeypatch, *[f"chunk {i}" for i in range(8)])

    async def go():
        return [a async for a in tts.stream_speech(None, "key", _no_text())]

    out = run(go())

    assert out == [f"chunk {i}" for i in range(8)]
    assert peak == tts.MAX_IN_FLIGHT


def test_cancelling_the_consumer_cancels_pending_synthesis_and_the_text_stream(
    monkeypatch,
):
    state = {"synth_cleaned": False, "text_cleaned": False}

    async def fake_synth(session, api_key, text, voice_id):
        try:
            await asyncio.sleep(30)
        finally:
            state["synth_cleaned"] = True
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)

    async def text():
        try:
            yield "Hello there, friend. "
            await asyncio.sleep(30)
            yield "never arrives."
        finally:
            state["text_cleaned"] = True

    async def go():
        async def consume():
            async for _ in tts.stream_speech(None, "key", text()):
                pass  # pragma: no cover - synth never finishes

        task = asyncio.create_task(consume())
        # Let the first chunk reach synthesis, then walk away mid-turn.
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())

    assert state["synth_cleaned"]
    assert state["text_cleaned"]


def test_a_synth_error_propagates_to_the_consumer(monkeypatch):
    synth_tasks: list[asyncio.Task] = []

    async def fake_synth(session, api_key, text, voice_id):
        synth_tasks.append(asyncio.current_task())
        if text == "second":
            raise tts.TTSError("boom")
        await asyncio.sleep(0.05)
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    _fake_chunks(monkeypatch, "first", "second", "third")

    async def go():
        got = []
        with pytest.raises(tts.TTSError, match="boom"):
            async for audio in tts.stream_speech(None, "key", _no_text()):
                got.append(audio)
        return got

    got = run(go())

    assert got == ["first"]
    assert synth_tasks and all(t.done() for t in synth_tasks)
