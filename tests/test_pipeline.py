"""Turn behaviour: streaming, interruption, and how failures degrade.

The upstream services are replaced with fakes, so this asserts orchestration -
what reaches the browser and what ends up in history - without any network.
"""

from __future__ import annotations

import asyncio

import pytest

from meraki.config import ApiKeys
from meraki.pipeline import TurnPipeline
from meraki.services import llm, tts
from meraki.session import Conversation

KEYS = ApiKeys(deepgram="dg", ollama="ol", murf="mu")


class Recorder:
    """Collects the frames a turn sends to the browser."""

    def __init__(self):
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames]

    def of(self, kind: str) -> list[dict]:
        return [f for f in self.frames if f["type"] == kind]

    def text(self) -> str:
        return "".join(f["text"] for f in self.of("reply_chunk"))


def fake_llm(tokens, *, delay=0.0, error=None):
    async def stream_reply(http, key, history, user_text):
        for token in tokens:
            if delay:
                await asyncio.sleep(delay)
            yield token
        if error:
            raise error

    return stream_reply


def fake_tts(*, error=None):
    async def stream_speech(http, key, text_stream, voice_id=None):
        # Drain the text the way the real one does, so the LLM actually runs.
        async for _ in tts.chunk_stream(text_stream):
            if error:
                raise error
            yield "QUJD"

    return stream_speech


@pytest.fixture(autouse=True)
def _patch_services(monkeypatch):
    """Default to well-behaved fakes; individual tests override."""
    monkeypatch.setattr(llm, "stream_reply", fake_llm(["Hi there. "]))
    monkeypatch.setattr(tts, "stream_speech", fake_tts())


def run_turn(convo, text="hello"):
    send = Recorder()
    pipeline = TurnPipeline(send=send, http=None, keys=KEYS, conversation=convo)
    asyncio.run(pipeline.run(text))
    return send


# --- the happy path ----------------------------------------------------------


def test_a_turn_streams_text_then_audio_then_finishes():
    send = run_turn(Conversation())
    types = send.types()

    assert types[0] == "thinking"
    assert "reply_chunk" in types
    assert "audio" in types
    assert types[-1] == "speech_done"
    assert send.of("reply_done")[0]["text"] == "Hi there."


def test_both_sides_of_the_conversation_are_recorded(monkeypatch):
    monkeypatch.setattr(llm, "stream_reply", fake_llm(["Sure thing."]))
    convo = Conversation()
    run_turn(convo, "what's up")

    assert [(t.role, t.content) for t in convo.turns] == [
        ("user", "what's up"),
        ("assistant", "Sure thing."),
    ]


def test_the_current_question_is_not_duplicated_into_history(monkeypatch):
    """It goes in the prompt, so it must not also be in the prior-turns list."""
    seen = {}

    async def capture(http, key, history, user_text):
        seen["history"] = list(history)
        seen["user_text"] = user_text
        yield "ok"

    monkeypatch.setattr(llm, "stream_reply", capture)

    convo = Conversation()
    convo.add("user", "earlier question")
    convo.add("assistant", "earlier answer")
    run_turn(convo, "new question")

    assert seen["user_text"] == "new question"
    assert seen["history"] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]


def test_audio_chunks_are_numbered_in_order(monkeypatch):
    monkeypatch.setattr(
        llm, "stream_reply", fake_llm(["One. ", "Two. ", "Three. ", "Four. "])
    )
    send = run_turn(Conversation())
    assert [f["seq"] for f in send.of("audio")] == list(
        range(len(send.of("audio")))
    )


# --- barge-in ----------------------------------------------------------------


def test_an_interrupted_turn_still_remembers_what_was_said(monkeypatch):
    """Cancelling mid-reply must not lose the context the user already heard."""
    monkeypatch.setattr(
        llm, "stream_reply", fake_llm(["Once ", "upon ", "a ", "time"], delay=0.05)
    )
    convo = Conversation()
    send = Recorder()
    pipeline = TurnPipeline(send=send, http=None, keys=KEYS, conversation=convo)

    async def scenario():
        turn = asyncio.create_task(pipeline.run("tell me a story"))
        await asyncio.sleep(0.12)  # let a couple of tokens through
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())

    assistant = [t for t in convo.turns if t.role == "assistant"]
    assert len(assistant) == 1
    partial = assistant[0].content
    assert partial  # something was captured
    assert "time" not in partial  # but not the whole thing


def test_cancelling_before_any_token_records_no_empty_reply(monkeypatch):
    monkeypatch.setattr(llm, "stream_reply", fake_llm(["later"], delay=0.5))
    convo = Conversation()
    pipeline = TurnPipeline(
        send=Recorder(), http=None, keys=KEYS, conversation=convo
    )

    async def scenario():
        turn = asyncio.create_task(pipeline.run("hi"))
        await asyncio.sleep(0.02)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())

    assert [t.role for t in convo.turns] == ["user"]


# --- failure modes -----------------------------------------------------------


def test_a_model_failure_becomes_a_typed_error_frame(monkeypatch):
    monkeypatch.setattr(
        llm, "stream_reply", fake_llm([], error=llm.LLMError("Ollama rejected that API key."))
    )
    send = run_turn(Conversation())

    errors = send.of("error")
    assert errors and errors[0]["code"] == "llm"
    assert "rejected" in errors[0]["message"]


def test_a_voice_failure_still_releases_the_ui(monkeypatch):
    """Text is already on screen; the browser must not wait on audio forever."""
    monkeypatch.setattr(llm, "stream_reply", fake_llm(["Here you go. "]))
    monkeypatch.setattr(tts, "stream_speech", fake_tts(error=tts.TTSError("Murf is down.")))

    send = run_turn(Conversation())

    assert send.of("error")[0]["code"] == "tts"
    assert "speech_done" in send.types()  # the release


def test_an_unexpected_crash_does_not_leak_details(monkeypatch):
    monkeypatch.setattr(
        llm, "stream_reply", fake_llm([], error=ValueError("secret internal detail"))
    )
    send = run_turn(Conversation())

    errors = send.of("error")
    assert errors[0]["code"] == "internal"
    assert "secret" not in errors[0]["message"]


def test_no_audio_frame_is_sent_when_the_model_says_nothing(monkeypatch):
    monkeypatch.setattr(llm, "stream_reply", fake_llm([]))
    send = run_turn(Conversation())

    assert send.of("audio") == []
    assert "speech_done" in send.types()
