"""Tests for the pure logic: chunking, session store, key resolution.

Network-facing code is exercised through fakes rather than live calls, so the
suite runs offline and without any API keys.
"""

from __future__ import annotations

import asyncio

import pytest

from meraki.config import CHUNK_MAX_CHARS, ApiKeys
from meraki.services.tts import chunk_stream
from meraki.session import SessionStore


# --- helpers -----------------------------------------------------------------


async def _tokens(text: str, size: int = 7):
    """Emit text in small pieces, the way a model streams."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def _collect(text: str, size: int = 7) -> list[str]:
    return [chunk async for chunk in chunk_stream(_tokens(text, size))]


def chunks_of(text: str, size: int = 7) -> list[str]:
    return asyncio.run(_collect(text, size))


# --- chunking ----------------------------------------------------------------


def test_chunking_preserves_every_character():
    text = (
        "Right, here's the thing. Voice agents live or die on latency, "
        "not on how clever the model is. Keep it short and it feels alive."
    )
    joined = " ".join(chunks_of(text))
    assert joined.split() == text.split()


def test_first_chunk_is_short_so_audio_starts_early():
    text = (
        "Yes, that works. The trick is to send the first clause to the "
        "synthesiser before the model has finished the rest of the sentence."
    )
    chunks = chunks_of(text)
    assert len(chunks) > 1
    # Fast first sound is the whole point of chunking.
    assert len(chunks[0]) < 120


def test_chunks_split_on_sentence_boundaries():
    chunks = chunks_of("One thing here. Another thing there. A third thing now.")
    assert chunks[0].endswith(".")


def test_no_chunk_exceeds_the_cap_even_without_punctuation():
    text = "word " * 300
    for chunk in chunks_of(text):
        assert len(chunk) <= CHUNK_MAX_CHARS


def test_a_tight_em_dash_still_starts_the_audio_early():
    """Measured case: "hot pan-about thirty seconds" fell back to one chunk."""
    text = "Fry it on high heat in a hot pan—about thirty seconds is plenty."
    chunks = chunks_of(text)
    assert len(chunks) > 1
    assert chunks[0].endswith("—")


def test_a_number_is_never_split_across_chunks():
    """Commas require trailing space, so "1,500" is not a clause boundary."""
    text = "It costs about 1,500 rupees, which is honestly a bargain for that."
    chunks = chunks_of(text)
    assert any("1,500" in chunk for chunk in chunks), chunks


def test_short_reply_emits_one_chunk():
    assert chunks_of("Sure.") == ["Sure."]


def test_empty_stream_emits_nothing():
    assert chunks_of("") == []


def test_whitespace_only_stream_emits_nothing():
    assert chunks_of("   \n  ") == []


# --- session store -----------------------------------------------------------


def test_history_round_trips_as_chat_messages():
    store = SessionStore()
    convo = store.get("abc")
    convo.add("user", "hello")
    convo.add("assistant", "hi there")

    assert convo.as_messages() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_same_id_returns_the_same_conversation():
    store = SessionStore()
    store.get("abc").add("user", "remember me")
    assert len(store.get("abc").turns) == 1


def test_history_is_capped():
    from meraki.config import MAX_HISTORY_MESSAGES

    convo = SessionStore().get("abc")
    for i in range(MAX_HISTORY_MESSAGES + 25):
        convo.add("user", f"turn {i}")
    assert len(convo.turns) == MAX_HISTORY_MESSAGES
    # The cap must drop the oldest, not the newest.
    assert convo.turns[-1].content == f"turn {MAX_HISTORY_MESSAGES + 24}"


def test_store_evicts_least_recently_used():
    store = SessionStore(max_sessions=3)
    for name in ("a", "b", "c"):
        store.get(name).add("user", name)
    store.get("a")  # refresh 'a' so 'b' becomes the coldest
    store.get("d").add("user", "d")

    assert len(store) == 3
    assert store.get("b").turns == []  # evicted, comes back empty


def test_reading_an_unknown_session_creates_nothing():
    """A GET must not mutate the store."""
    store = SessionStore(max_sessions=5)
    store.get("real").add("user", "important")

    for i in range(20):
        assert store.peek(f"junk-{i}") is None

    assert len(store) == 1
    assert store.peek("real").turns, "a real conversation was evicted by reads"


def test_peek_returns_an_existing_conversation():
    store = SessionStore()
    store.get("abc").add("user", "hello")
    assert store.peek("abc").turns[0].content == "hello"


def test_clear_removes_a_session():
    store = SessionStore()
    store.get("abc").add("user", "hello")
    assert store.clear("abc") is True
    assert store.clear("abc") is False
    assert store.get("abc").turns == []


# --- key handling ------------------------------------------------------------


def env(monkeypatch, **values):
    for name in ("DEEPGRAM_API_KEY", "OLLAMA_API_KEY", "MURF_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_keys_are_read_from_the_environment(monkeypatch):
    env(monkeypatch, DEEPGRAM_API_KEY="dg", OLLAMA_API_KEY="ol", MURF_API_KEY="mu")
    keys = ApiKeys.from_env()
    assert (keys.deepgram, keys.ollama, keys.murf) == ("dg", "ol", "mu")
    assert keys.missing() == []


def test_surrounding_whitespace_is_stripped(monkeypatch):
    """Pasting into a .env commonly leaves a trailing space."""
    env(monkeypatch, DEEPGRAM_API_KEY="  dg  ", OLLAMA_API_KEY="ol", MURF_API_KEY="mu")
    assert ApiKeys.from_env().deepgram == "dg"


def test_a_visitor_can_bring_their_own_keys(monkeypatch):
    env(monkeypatch)
    keys = ApiKeys.from_payload({"deepgram": "dg", "ollama": "ol", "murf": "mu"})
    assert keys.missing() == []
    assert keys.deepgram == "dg"


def test_uppercase_env_style_names_also_work(monkeypatch):
    env(monkeypatch)
    keys = ApiKeys.from_payload(
        {"DEEPGRAM_API_KEY": "dg", "OLLAMA_API_KEY": "ol", "MURF_API_KEY": "mu"}
    )
    assert keys.missing() == []


def test_a_visitors_key_wins_over_the_servers(monkeypatch):
    """Otherwise they would silently spend the owner's credits."""
    env(monkeypatch, DEEPGRAM_API_KEY="server", OLLAMA_API_KEY="server",
        MURF_API_KEY="server")
    keys = ApiKeys.from_payload({"deepgram": "mine"})
    assert keys.deepgram == "mine"


def test_the_server_fills_in_what_the_visitor_omits(monkeypatch):
    """A deployment with its own keys should just work with an empty dialog."""
    env(monkeypatch, DEEPGRAM_API_KEY="dg", OLLAMA_API_KEY="ol", MURF_API_KEY="mu")
    keys = ApiKeys.from_payload({})
    assert (keys.deepgram, keys.ollama, keys.murf) == ("dg", "ol", "mu")


def test_visitor_and_server_keys_can_be_mixed(monkeypatch):
    env(monkeypatch, DEEPGRAM_API_KEY="dg-server", OLLAMA_API_KEY="ol-server",
        MURF_API_KEY="mu-server")
    keys = ApiKeys.from_payload({"murf": "mu-mine"})
    assert keys.murf == "mu-mine"
    assert keys.deepgram == "dg-server"


def test_blank_payload_values_fall_back_rather_than_blanking(monkeypatch):
    """An empty settings field must not disable a working server key."""
    env(monkeypatch, DEEPGRAM_API_KEY="dg", OLLAMA_API_KEY="ol", MURF_API_KEY="mu")
    keys = ApiKeys.from_payload({"deepgram": "   ", "ollama": ""})
    assert keys.deepgram == "dg"
    assert keys.ollama == "ol"


def test_missing_keys_are_named_for_a_human(monkeypatch):
    env(monkeypatch, OLLAMA_API_KEY="ol")
    assert set(ApiKeys.from_payload({}).missing()) == {"Deepgram", "Murf"}


def test_nothing_anywhere_reports_all_three(monkeypatch):
    env(monkeypatch)
    assert set(ApiKeys.from_payload({}).missing()) == {"Deepgram", "Ollama", "Murf"}


def test_keys_cannot_be_mutated_after_construction(monkeypatch):
    """Frozen, so one connection's keys can never be rewritten by another."""
    env(monkeypatch)
    keys = ApiKeys.from_payload({"deepgram": "dg", "ollama": "ol", "murf": "mu"})
    with pytest.raises(Exception):
        keys.deepgram = "someone else's"


def test_no_module_holds_keys_of_its_own():
    """The original build kept one global dict and handed it to every visitor.

    Keys must live on the connection object and nowhere else.
    """
    from meraki import config, main

    for module in (config, main):
        for name in dir(module):
            if name.startswith("__"):
                continue
            value = getattr(module, name)
            assert not isinstance(value, ApiKeys), (
                f"{module.__name__}.{name} holds credentials at module level"
            )


# --- protocol ----------------------------------------------------------------


def test_every_frame_carries_a_type():
    from meraki import protocol

    frames = [
        protocol.ready(),
        protocol.partial("x"),
        protocol.final("x"),
        protocol.thinking(),
        protocol.reply_chunk("x"),
        protocol.reply_done("x"),
        protocol.audio(0, "aGk="),
        protocol.speech_done(),
        protocol.interrupted(),
        protocol.error("code", "message"),
    ]
    assert all("type" in frame for frame in frames)
    assert protocol.error("c", "m")["fatal"] is False
    assert protocol.error("c", "m", fatal=True)["fatal"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
