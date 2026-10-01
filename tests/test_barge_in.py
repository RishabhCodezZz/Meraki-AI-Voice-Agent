"""Barge-in: when talking over the assistant must cut it off, and when not.

TTS finishes seconds before the browser has played the audio out, so a turn can
be long finished while Meraki is still speaking. Interrupting has to cover that
case too, without announcing an interruption for a reply that ended long ago.

Events are fed one at a time with a pause between, so a turn task the pump
created actually runs before the next event arrives, as it does with real speech.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from meraki import protocol
from meraki.main import _Connection

FIRST = "tell me a story"


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class FakeSpeech:
    def __init__(self):
        self.events: asyncio.Queue = asyncio.Queue()


class Scenario:
    """A connection whose turns are whatever `behaviour(conn, text)` says."""

    def __init__(self, behaviour):
        self.ws = FakeWebSocket()
        self.conn = _Connection(self.ws)
        self.conn._speech = FakeSpeech()
        self.conn._session_id = "test"
        self.started: list[tuple[str, int]] = []  # (text, frames sent by then)
        self.cancelled: list[str] = []

        async def run_turn(text):
            self.started.append((text, len(self.ws.sent)))
            try:
                await behaviour(self.conn, text)
            except asyncio.CancelledError:
                self.cancelled.append(text)
                raise

        self.conn._run_turn = run_turn

    async def feed(self, *events):
        for event in events:
            await self.conn._speech.events.put(event)
            await asyncio.sleep(0.01)

    def types(self) -> list[str]:
        return [frame["type"] for frame in self.ws.sent]


def run(behaviour, script):
    """Run `script(scenario)` with the event pump live; return the scenario."""
    scenario = Scenario(behaviour)

    async def main():
        pump = asyncio.create_task(scenario.conn._drain_speech_events())
        try:
            await script(scenario)
            await asyncio.sleep(0.05)  # let the pump finish what it was doing
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    asyncio.run(main())
    return scenario


async def blocks_forever(conn, text):
    await asyncio.Event().wait()


async def speaks_then_finishes(conn, text):
    await conn._send(protocol.audio(0, "QUJD"))


async def finishes_at_once(conn, text):
    return


async def thinks_only(conn, text):
    await conn._send(protocol.thinking())


@pytest.fixture
def clock(monkeypatch):
    """A frozen, adjustable `meraki.main._now`."""
    now = [0.0]
    monkeypatch.setattr("meraki.main._now", lambda: now[0])
    return now


# --- a turn that is still running --------------------------------------------


def test_two_word_partial_cancels_a_running_turn_and_says_interrupted(clock):
    async def script(s):
        await s.feed(
            {"kind": "final", "text": FIRST},
            {"kind": "partial", "text": "no wait"},
        )

    s = run(blocks_forever, script)

    assert "interrupted" in s.types()
    assert s.cancelled == [FIRST]


def test_a_one_word_final_interrupts_a_running_turn(clock):
    async def script(s):
        await s.feed(
            {"kind": "final", "text": FIRST},
            {"kind": "final", "text": "stop"},
        )

    s = run(blocks_forever, script)

    assert s.types().count("interrupted") == 1
    assert s.cancelled[0] == FIRST  # ("stop" is cancelled too, by teardown)
    assert [text for text, _ in s.started] == [FIRST, "stop"]


# --- a turn that finished while its audio is still playing -------------------


def test_two_word_partial_interrupts_audio_that_is_still_playing_after_the_turn_finished(clock):
    async def script(s):
        await s.feed(
            {"kind": "final", "text": FIRST},
            {"kind": "partial", "text": "no wait"},
        )

    s = run(speaks_then_finishes, script)

    assert "audio" in s.types()
    assert "interrupted" in s.types()
    assert s.cancelled == [], "the turn was already done; there is nothing to cancel"


def test_a_one_word_final_interrupts_playing_audio(clock):
    async def script(s):
        await s.feed(
            {"kind": "final", "text": FIRST},
            {"kind": "final", "text": "stop"},
        )

    s = run(speaks_then_finishes, script)

    assert [text for text, _ in s.started] == [FIRST, "stop"]
    stop_started_after = s.started[1][1]
    assert s.types().index("interrupted") < stop_started_after, (
        "the browser must be told to drop the old audio before the new turn begins"
    )


def test_interrupted_is_sent_once_per_utterance(clock):
    async def script(s):
        await s.feed({"kind": "final", "text": FIRST})
        await s.feed(
            {"kind": "partial", "text": "no wait"},
            {"kind": "partial", "text": "no wait hang"},
            {"kind": "partial", "text": "no wait hang on"},
        )

    s = run(speaks_then_finishes, script)

    assert s.types().count("interrupted") == 1


def test_nothing_is_interrupted_when_nothing_is_playing(clock):
    async def script(s):
        await s.feed(
            {"kind": "partial", "text": "no wait"},
            {"kind": "partial", "text": "no wait hang on"},
        )

    s = run(finishes_at_once, script)

    assert "interrupted" not in s.types()


def test_a_reply_with_no_audio_leaves_nothing_to_interrupt(clock):
    """A turn that failed before any audio has no tail playing in the browser."""

    async def script(s):
        await s.feed(
            {"kind": "final", "text": FIRST},
            {"kind": "partial", "text": "no wait"},
        )

    s = run(finishes_at_once, script)

    assert "interrupted" not in s.types()


def test_a_new_turn_without_audio_forgets_the_previous_turns_audio(clock):
    """Audio from turn one must not make a silent turn two look interruptible.

    Turn one's audio played out long ago (so the next final announces nothing
    and the flag survives), then turn two starts and opens a fresh grace window.
    """
    turns = [speaks_then_finishes, thinks_only]

    async def behaviour(conn, text):
        await turns.pop(0)(conn, text)

    async def script(s):
        await s.feed({"kind": "final", "text": FIRST})
        clock[0] = 100.0  # turn one's audio is long done
        await s.feed({"kind": "final", "text": "and another thing"})
        # Inside turn two's grace window, where a stale flag would count.
        await s.feed({"kind": "partial", "text": "no wait"})

    s = run(behaviour, script)

    assert "audio" in s.types()
    assert "interrupted" not in s.types()


def test_long_finished_audio_does_not_trigger_a_spurious_interrupted(clock):
    async def script(s):
        await s.feed({"kind": "final", "text": FIRST})
        clock[0] = 100.0  # far past the end of playback plus grace
        await s.feed({"kind": "partial", "text": "no wait"})

    s = run(speaks_then_finishes, script)

    assert "audio" in s.types()
    assert "interrupted" not in s.types()


# --- cancellation is safe ----------------------------------------------------


def test_cancelling_the_pump_while_it_awaits_a_turn_is_not_swallowed():
    """A turn slow to unwind must not turn a cancel aimed at us into a no-op."""

    async def scenario():
        conn = _Connection(FakeWebSocket())

        async def slow_to_unwind():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
                raise

        conn._turn = asyncio.create_task(slow_to_unwind())
        await asyncio.sleep(0)  # let it start waiting

        waiter = asyncio.create_task(conn._cancel_turn(notify=False))
        await asyncio.sleep(0.05)  # now inside the 0.2s unwind
        waiter.cancel()
        await asyncio.wait({waiter}, timeout=1)

        assert waiter.done(), "the cancel was swallowed and the waiter hung"
        assert waiter.cancelled(), "the waiter finished normally instead of cancelling"

    asyncio.run(scenario())


def test_close_still_closes_the_socket_when_speech_close_raises():
    class BrokenSpeech:
        async def close(self):
            raise RuntimeError("deepgram went away badly")

    async def scenario():
        ws = FakeWebSocket()
        conn = _Connection(ws)
        conn._speech = BrokenSpeech()
        await conn.close()
        return ws

    assert asyncio.run(scenario()).closed is True
