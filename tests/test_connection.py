"""Connection-level behaviour: what the browser is told, and when.

The event pump is the only thing feeding a conversation. If it stops without
saying so, the socket stays open, the browser keeps streaming audio, and the UI
sits on "Listening" forever. These pin down that it always says so.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from meraki.main import (
    BARGE_IN_MIN_WORDS,
    ECHO_CHARS_PER_SECOND,
    ECHO_GRACE_SECONDS,
    _Connection,
    looks_like_echo,
)


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class FakeSpeech:
    def __init__(self, events):
        self.events: asyncio.Queue = asyncio.Queue()
        self._seed = events

    async def fill(self):
        for event in self._seed:
            await self.events.put(event)


def drive(events, *, spoken="", turn_factory=None, now=0.0):
    """Run the pump over a fixed list of STT events and collect what was sent.

    The clock is frozen at `now`. A reply given as `spoken` is treated as having
    begun at t=0, so with no audio yet the echo window closes at
    ECHO_GRACE_SECONDS; pass a later `now` to look at it after that.
    """
    ws = FakeWebSocket()
    conn = _Connection(ws)
    conn._speech = FakeSpeech(events)
    conn._spoken = spoken
    conn._echo_deadline = ECHO_GRACE_SECONDS if spoken else float("-inf")
    conn._session_id = "test"
    started: list[str] = []

    async def fake_run_turn(text):
        started.append(text)
        if turn_factory:
            await turn_factory()

    conn._run_turn = fake_run_turn

    async def scenario():
        await conn._speech.fill()
        try:
            await asyncio.wait_for(conn._drain_speech_events(), timeout=2)
        except asyncio.TimeoutError:
            pass
        # Let any turn task the pump created actually run.
        await asyncio.sleep(0)
        if conn._turn:
            await conn._turn

    with mock.patch("meraki.main._now", lambda: now):
        asyncio.run(scenario())
    return ws.sent, started


def types(sent):
    return [frame["type"] for frame in sent]


# --- the stream going away ---------------------------------------------------


def test_a_dropped_transcription_stream_is_reported():
    """Teardown cancels the pump first, so 'closed' here means Deepgram left."""
    sent, _ = drive([{"kind": "closed"}])

    errors = [f for f in sent if f["type"] == "error"]
    assert errors, "the browser was told nothing"
    assert errors[0]["fatal"] is True
    assert errors[0]["code"] == "stt"


def test_a_crash_in_the_pump_still_reaches_the_browser():
    """A bare exception here used to kill the task and notify nobody."""
    sent, _ = drive([{"kind": "partial"}])  # no "text" key -> KeyError

    errors = [f for f in sent if f["type"] == "error"]
    assert errors and errors[0]["fatal"] is True


def test_an_upstream_error_is_forwarded_without_being_fatal():
    """A transient STT error should not tear the conversation down."""
    sent, _ = drive([{"kind": "error", "message": "rate limited"}, {"kind": "closed"}])

    first = [f for f in sent if f["type"] == "error"][0]
    assert first["fatal"] is False
    assert "rate limited" in first["message"]


# --- turns -------------------------------------------------------------------


def test_a_final_transcript_starts_a_turn():
    sent, started = drive([{"kind": "final", "text": "what is the time"}, {"kind": "closed"}])

    assert started == ["what is the time"]
    assert "final" in types(sent)


def test_partials_are_forwarded_but_start_nothing():
    sent, started = drive([{"kind": "partial", "text": "what is"}, {"kind": "closed"}])

    assert started == []
    assert "partial" in types(sent)


def test_the_assistant_hearing_itself_is_dropped_entirely():
    spoken = "The fastest way to cook an egg is to fry it."
    sent, started = drive(
        [{"kind": "final", "text": "the fastest way to cook an egg"}, {"kind": "closed"}],
        spoken=spoken,
    )

    assert started == [], "echo must not start a turn"
    assert "final" not in types(sent), "echo must not appear as the user's speech"


def test_real_speech_still_interrupts_while_it_is_talking():
    spoken = "The fastest way to cook an egg is to fry it."
    sent, started = drive(
        [{"kind": "final", "text": "no wait hang on"}, {"kind": "closed"}],
        spoken=spoken,
    )

    assert started == ["no wait hang on"]


def test_a_single_word_partial_does_not_trip_barge_in():
    """One syllable of room noise should not cut the assistant off.

    Only meaningful with a turn to cut off: with nothing running, no word count
    could ever produce an interruption and the test would pass regardless.
    """
    assert BARGE_IN_MIN_WORDS == 2
    started, finished = [], []

    async def a_turn_in_progress():
        started.append(True)
        await asyncio.sleep(0.05)
        finished.append(True)

    sent, _ = drive(
        [
            {"kind": "final", "text": "tell me a story"},
            {"kind": "partial", "text": "um"},
            {"kind": "closed"},
        ],
        turn_factory=a_turn_in_progress,
    )

    assert "interrupted" not in types(sent)
    assert started == [True] and finished == [True], "the turn was cancelled"


# --- echo expires ------------------------------------------------------------

REPLY = "The fastest way to cook an egg is."  # 40 chars is plenty for these


def test_echo_still_counts_while_the_reply_is_playing():
    sent, started = drive(
        [{"kind": "final", "text": "the fastest way to cook"}, {"kind": "closed"}],
        spoken=REPLY,
        now=ECHO_GRACE_SECONDS - 0.5,
    )

    assert started == []


def test_echo_stops_counting_after_the_reply_has_finished_playing():
    """Otherwise a user repeating a phrase from a long-finished reply is ignored."""
    sent, started = drive(
        [{"kind": "final", "text": "the fastest way to cook"}, {"kind": "closed"}],
        spoken=REPLY,
        now=ECHO_GRACE_SECONDS + 0.5,
    )

    assert started == ["the fastest way to cook"]


def test_echo_window_stretches_with_the_audio_that_has_started():
    """A long reply keeps playing well past the grace period after the text."""
    conn = _Connection(FakeWebSocket())
    conn._spoken = "x" * int(ECHO_CHARS_PER_SECOND * 10)  # ~10s of speech
    conn._first_audio_at = 100.0

    with mock.patch("meraki.main._now", lambda: 100.0 + 10 + ECHO_GRACE_SECONDS - 0.1):
        assert conn._echo_active()
    with mock.patch("meraki.main._now", lambda: 100.0 + 10 + ECHO_GRACE_SECONDS + 0.1):
        assert not conn._echo_active()


def test_sending_a_turn_opens_and_stretches_the_echo_window():
    """thinking opens the window, audio anchors it, reply text sets its length."""
    conn = _Connection(FakeWebSocket())
    clock = [50.0]

    async def send_all():
        await conn._send({"type": "thinking"})
        await conn._send({"type": "reply_chunk", "text": "x" * int(ECHO_CHARS_PER_SECOND * 5)})
        clock[0] = 51.0
        await conn._send({"type": "audio", "seq": 0, "data": ""})
        clock[0] = 52.0
        await conn._send({"type": "audio", "seq": 1, "data": ""})  # not the first

    with mock.patch("meraki.main._now", lambda: clock[0]):
        asyncio.run(send_all())
        assert conn._first_audio_at == 51.0
        clock[0] = 51.0 + 5 + ECHO_GRACE_SECONDS - 0.1
        assert conn._echo_active()
        clock[0] = 51.0 + 5 + ECHO_GRACE_SECONDS + 0.1
        assert not conn._echo_active()


def test_nothing_is_echo_on_a_fresh_connection():
    assert not _Connection(FakeWebSocket())._echo_active()


# --- echo helper -------------------------------------------------------------


def test_echo_matching_ignores_case_and_punctuation():
    assert looks_like_echo("The trick, IS to send!", "the trick is to send it early")


def test_nothing_is_echo_before_anything_was_said():
    assert not looks_like_echo("hello there", "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
