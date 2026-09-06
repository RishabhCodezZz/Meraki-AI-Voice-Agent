"""Deepgram turn assembly.

The two flags are easy to confuse and getting it wrong is subtle rather than
loud - acting on is_final alone chops long sentences into fragments, and each
fragment fires its own reply. These pin the behaviour down.

  is_final     - this segment is settled and will not be revised
  speech_final - endpointing fired; the speaker finished a thought
"""

from __future__ import annotations

import asyncio

from meraki.services.stt import SpeechStream


def result(transcript: str, *, is_final: bool = False, speech_final: bool = False):
    return {
        "channel": {"alternatives": [{"transcript": transcript}]},
        "is_final": is_final,
        "speech_final": speech_final,
    }


def feed(payloads: list[dict]) -> list[dict]:
    """Push payloads through a stream and collect the events it emits."""
    stream = SpeechStream(http=None, api_key="")

    async def scenario():
        for payload in payloads:
            await stream._handle(payload)
        events = []
        while not stream.events.empty():
            events.append(stream.events.get_nowait())
        return events

    return asyncio.run(scenario())


def finals(events: list[dict]) -> list[str]:
    return [e["text"] for e in events if e["kind"] == "final"]


def partials(events: list[dict]) -> list[str]:
    return [e["text"] for e in events if e["kind"] == "partial"]


# --- assembly ----------------------------------------------------------------

def test_settled_segments_join_into_one_utterance():
    """A sentence split across two settled segments must arrive as one turn."""
    events = feed(
        [
            result("what is", is_final=False),
            result("what is the", is_final=True),
            result("weather like today", is_final=True, speech_final=True),
        ]
    )

    assert finals(events) == ["what is the weather like today"]


def test_nothing_is_final_until_endpointing_fires():
    """is_final on its own must not start a reply."""
    events = feed(
        [
            result("this is a long", is_final=True),
            result("sentence that keeps going", is_final=True),
        ]
    )

    assert finals(events) == []
    assert partials(events)  # still shown live


def test_interim_text_is_shown_but_not_acted_on():
    events = feed([result("hel", is_final=False), result("hello", is_final=False)])

    assert finals(events) == []
    assert partials(events) == ["hel", "hello"]


def test_interim_text_includes_the_settled_prefix():
    """The live caption should read as a whole sentence, not just the tail."""
    events = feed(
        [
            result("tell me about", is_final=True),
            result("the weather", is_final=False),
        ]
    )

    assert partials(events)[-1] == "tell me about the weather"


def test_consecutive_utterances_do_not_bleed_together():
    events = feed(
        [
            result("first question", is_final=True, speech_final=True),
            result("second question", is_final=True, speech_final=True),
        ]
    )

    assert finals(events) == ["first question", "second question"]


def test_empty_transcripts_are_ignored():
    events = feed(
        [
            result("", is_final=False),
            result("", is_final=True, speech_final=True),
        ]
    )

    assert events == []


def test_endpointing_on_silence_does_not_emit_an_empty_turn():
    """Endpointing can fire with nothing buffered; that is not a turn."""
    events = feed([result("", is_final=True, speech_final=True)])

    assert finals(events) == []


def test_an_error_payload_becomes_an_error_event():
    events = feed([{"type": "Error", "description": "rate limited"}])

    assert events == [{"kind": "error", "message": "rate limited"}]


def test_malformed_payloads_are_survived():
    assert feed([{}, {"channel": {}}, {"channel": {"alternatives": []}}]) == []
