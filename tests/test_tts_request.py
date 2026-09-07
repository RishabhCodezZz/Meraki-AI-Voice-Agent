"""What we actually send to Murf, and how we recover when it says no.

Uses a fake aiohttp session so the shape of the request is asserted without a
key or a network call.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from meraki.config import TTS_SAMPLE_RATE, VOICE_ID, VOICE_STYLE
from meraki.services import tts


class _Content:
    """Stands in for aiohttp's chunked response body."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status: int, chunks: list[bytes] | None = None, body: str = ""):
        self.status = status
        self.content = _Content(chunks if chunks is not None else [b"mp3-bytes"])
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    """Records POSTs and replays a scripted list of responses."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.posts: list[dict] = []
        self.urls: list[str] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        self.urls.append(url)
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_style_cache():
    """The supported-style flag is module state; keep tests independent."""
    tts._style_supported = True
    yield
    tts._style_supported = True


def run(coro):
    return asyncio.run(coro)


def test_streamed_chunks_are_joined_and_base64_encoded():
    """The body arrives in pieces; all of them must reach the browser."""
    session = _FakeSession([_FakeResponse(200, chunks=[b"abc", b"def", b"ghi"])])

    result = run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    assert base64.b64decode(result) == b"abcdefghi"


def test_the_streaming_endpoint_is_used():
    """Not /v1/speech/generate - that took 2865ms where this takes ~500ms."""
    from meraki.config import MURF_STREAM_URL

    session = _FakeSession([_FakeResponse(200)])
    run(tts.synthesize(session, "key", "Hello.", VOICE_ID))

    assert session.urls == [MURF_STREAM_URL]


def test_an_empty_body_is_an_error_not_silent_success():
    session = _FakeSession([_FakeResponse(200, chunks=[])])
    with pytest.raises(tts.TTSError, match="no audio"):
        run(tts.synthesize(session, "key", "Hello.", VOICE_ID))


def test_request_carries_model_voice_style_and_sample_rate():
    from meraki.config import MURF_MODEL

    session = _FakeSession([_FakeResponse(200)])
    run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    sent = session.posts[0]
    assert sent["model"] == MURF_MODEL
    assert sent["style"] == VOICE_STYLE
    assert sent["sampleRate"] == TTS_SAMPLE_RATE
    assert sent["voiceId"] == VOICE_ID
    assert sent["format"] == "MP3"


def test_unsupported_style_is_dropped_and_retried():
    """A voice that rejects the style should still speak, just plainly."""
    session = _FakeSession(
        [
            _FakeResponse(400, body="style not supported for this voice"),
            _FakeResponse(200),
        ]
    )

    result = run(tts.synthesize(session, "key", "Hello there.", VOICE_ID))

    assert result
    assert len(session.posts) == 2
    assert "style" in session.posts[0]
    assert "style" not in session.posts[1]


def test_style_is_not_retried_once_known_unsupported():
    """The first rejection is remembered, so we stop paying for a failed call."""
    first = _FakeSession(
        [
            _FakeResponse(400, body="style not supported"),
            _FakeResponse(200),
        ]
    )
    run(tts.synthesize(first, "key", "One.", VOICE_ID))

    second = _FakeSession([_FakeResponse(200)])
    run(tts.synthesize(second, "key", "Two.", VOICE_ID))

    assert "style" not in second.posts[0]
    assert len(second.posts) == 1


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
