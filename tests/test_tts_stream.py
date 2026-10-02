"""Delivery order and timing of ``stream_speech``.

``synthesize`` is replaced with a fake that sleeps, so these assert when audio
becomes available rather than what Murf returns.
"""

from __future__ import annotations

import asyncio

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

    release = asyncio.Event()

    async def text():
        yield "Hello there, friend. "
        # The second token is not available until the consumer has been handed
        # the first audio. No clock involved: if delivery waits for the text to
        # end, the two wait on each other and the timeout below fires.
        await release.wait()
        yield "More text here."

    async def go():
        out = []
        released_after_first_audio = False
        async for audio in tts.stream_speech(None, "key", text()):
            if not out:
                released_after_first_audio = not release.is_set()
                release.set()
            out.append(audio)
        return released_after_first_audio, out

    # The timeout only bounds a failure; it is never reached when this passes.
    first_before_second_token, out = run(asyncio.wait_for(go(), timeout=10))

    assert first_before_second_token
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


def _leftover_tasks():
    """Every task still alive besides the one running this check."""
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


def test_cancelling_the_consumer_cancels_pending_synthesis_and_the_text_stream(
    monkeypatch,
):
    cleaned_synth: set[str] = set()
    state = {"text_cleaned": False}

    async def fake_synth(session, api_key, text, voice_id):
        try:
            await asyncio.sleep(30)
        finally:
            cleaned_synth.add(text)
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)

    async def one_token_per_chunk(stream):
        async for token in stream:
            yield token

    monkeypatch.setattr(tts, "chunk_stream", one_token_per_chunk)

    async def text():
        try:
            yield "a"
            yield "b"
            await asyncio.sleep(30)
            yield "never arrives"
        finally:
            state["text_cleaned"] = True

    async def go():
        async def consume():
            async for _ in tts.stream_speech(None, "key", text()):
                pass  # pragma: no cover - synth never finishes

        task = asyncio.create_task(consume())
        # Let both chunks reach synthesis, then walk away mid-turn. The consumer
        # only ever awaits "a", so "b" must be cancelled by stream_speech itself.
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Checked here, not after asyncio.run(), which cancels leftovers itself
        # and would hide a leak.
        assert cleaned_synth == {"a", "b"}
        assert state["text_cleaned"]
        assert _leftover_tasks() == []

    run(go())


def test_a_synth_error_propagates_to_the_consumer(monkeypatch):
    synth_tasks: list[asyncio.Task] = []

    async def fake_synth(session, api_key, text, voice_id):
        synth_tasks.append(asyncio.current_task())
        if text == "second":
            raise tts.TTSError("boom")
        # "third" only finishes if stream_speech cancels it.
        await asyncio.sleep(0.05 if text == "first" else 30)
        return text

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    _fake_chunks(monkeypatch, "first", "second", "third")

    async def go():
        got = []
        with pytest.raises(tts.TTSError, match="boom"):
            async for audio in tts.stream_speech(None, "key", _no_text()):
                got.append(audio)

        # Checked inside the loop for the same reason as the cancel test.
        assert got == ["first"]
        assert len(synth_tasks) == 3
        assert all(t.done() for t in synth_tasks)
        assert _leftover_tasks() == []

    run(go())
