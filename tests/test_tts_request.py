"""What we actually send to Murf, and how we recover when it says no.

Uses a fake aiohttp session so the shape of the request is asserted without a
key or a network call.
"""

from __future__ import annotations

import asyncio

import pytest

from meraki.config import TTS_SAMPLE_RATE, VOICE_ID, VOICE_STYLE
from meraki.services import tts


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, body: str = ""):
        self.status = status
        self._payload = payload or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._body

    async def read(self):
        return b"mp3-bytes"


class _FakeSession:
    """Records POSTs and replays a scripted list of responses."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return self._responses.pop(0)

    def get(self, url, timeout=None):
        self.gets.append(url)
        return _FakeResponse(200)


@pytest.fixture(autouse=True)
def _reset_style_cache():
    """The supported-style flag is module state; keep tests independent."""
    tts._style_supported = True
    yield
    tts._style_supported = True


def run(coro):
    return asyncio.run(coro)


def test_request_asks_for_inline_base64_audio():
    """No second round trip to download the MP3 - that was pure latency."""
    session = _FakeSession([_FakeResponse(200, {"encodedAudio": "QUJD"})])

    result = run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    assert result == "QUJD"
    assert session.posts[0]["encodeAsBase64"] is True
    assert session.gets == []  # nothing downloaded


def test_request_carries_voice_style_and_sample_rate():
    session = _FakeSession([_FakeResponse(200, {"encodedAudio": "QUJD"})])

    run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    sent = session.posts[0]
    assert sent["style"] == VOICE_STYLE
    assert sent["sampleRate"] == TTS_SAMPLE_RATE
    assert sent["voiceId"] == VOICE_ID
    assert sent["format"] == "MP3"


def test_unsupported_style_is_dropped_and_retried():
    """A voice that rejects the style should still speak, just plainly."""
    session = _FakeSession(
        [
            _FakeResponse(400, body="style not supported for this voice"),
            _FakeResponse(200, {"encodedAudio": "QUJD"}),
        ]
    )

    result = run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    assert result == "QUJD"
    assert len(session.posts) == 2
    assert "style" in session.posts[0]
    assert "style" not in session.posts[1]


def test_style_is_not_retried_once_known_unsupported():
    """The first rejection is remembered, so we stop paying for a failed call."""
    first = _FakeSession(
        [
            _FakeResponse(400, body="style not supported"),
            _FakeResponse(200, {"encodedAudio": "QUJD"}),
        ]
    )
    run(tts.synthesize(first, "key", "One.", VOICE_ID))

    second = _FakeSession([_FakeResponse(200, {"encodedAudio": "QUJD"})])
    run(tts.synthesize(second, "key", "Two.", VOICE_ID))

    assert "style" not in second.posts[0]
    assert len(second.posts) == 1


def test_url_response_is_still_handled():
    """Older accounts may answer with a URL instead of inline audio."""
    session = _FakeSession([_FakeResponse(200, {"audioFile": "https://x/a.mp3"})])

    result = run(tts.synthesize(session, "key", "Hello.", VOICE_ID))

    assert result  # base64 of the downloaded bytes
    assert session.gets == ["https://x/a.mp3"]


def test_missing_key_fails_before_any_request():
    session = _FakeSession([])
    with pytest.raises(tts.TTSError, match="API key"):
        run(tts.synthesize(session, "", "Hello.", VOICE_ID))
    assert session.posts == []


def test_rejected_key_is_reported_plainly():
    session = _FakeSession([_FakeResponse(401, body="unauthorized")])
    with pytest.raises(tts.TTSError, match="rejected that API key"):
        run(tts.synthesize(session, "bad", "Hello.", VOICE_ID))


def test_model_and_voice_are_single_locked_values():
    """Visitors must not be able to pick these, so there is no list to pick from."""
    from meraki import config

    assert isinstance(config.MODEL, str)
    assert isinstance(config.VOICE_ID, str)
    assert not hasattr(config, "AVAILABLE_MODELS")
    assert not hasattr(config, "AVAILABLE_VOICES")
