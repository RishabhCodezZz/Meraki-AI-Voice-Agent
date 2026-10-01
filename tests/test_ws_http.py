"""The HTTP surface and the WebSocket handshake, driven through a real ASGI app.

Nothing here reaches the network: the lifespan creates an aiohttp session but
never connects, and where a handshake gets as far as Deepgram, `SpeechStream.start`
is replaced with one that fails.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import meraki.main as main
from meraki.config import ApiKeys
from meraki.security import origin_allowed
from meraki.services.stt import SpeechError
from meraki.session import sessions, valid_session_id


@pytest.fixture
def client():
    # As a context manager so the lifespan runs and `_http` exists.
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def keys_present(monkeypatch):
    """Server keys set, and the Deepgram step failing instead of dialling out."""
    for name in ("DEEPGRAM_API_KEY", "OLLAMA_API_KEY", "MURF_API_KEY"):
        monkeypatch.setenv(name, "test-key")

    async def refuse(self):
        raise SpeechError("x")

    monkeypatch.setattr(main.SpeechStream, "start", refuse)


@pytest.fixture
def seen_session_ids(monkeypatch):
    """Record the session id each connection settled on after its handshake."""
    ids: list[str] = []
    original = main._Connection._handshake

    async def recording(self):
        ok = await original(self)
        ids.append(self._session_id)
        return ok

    monkeypatch.setattr(main._Connection, "_handshake", recording)
    return ids


def _handshake(client, frame, headers=None):
    """Send `frame` as the first message; return the first reply."""
    with client.websocket_connect("/ws", headers=headers or {}) as ws:
        ws.send_json(frame)
        return ws.receive_json()


def _no_tracebacks(caplog):
    return [r for r in caplog.records if r.exc_info]


# --- malformed first frames ---------------------------------------------------


def test_non_dict_keys_in_the_handshake_is_a_typed_error(client, caplog):
    caplog.set_level(logging.DEBUG)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "config", "keys": "abc"})
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["fatal"] is True
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert _no_tracebacks(caplog) == []


def test_a_binary_first_frame_is_a_typed_error(client, caplog):
    caplog.set_level(logging.DEBUG)
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "handshake"
        assert reply["fatal"] is True
    assert _no_tracebacks(caplog) == []


def test_a_deeply_nested_first_frame_is_a_typed_error(client, caplog):
    caplog.set_level(logging.DEBUG)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("[" * 40000)
        reply = ws.receive_json()
        assert (reply["type"], reply["code"], reply["fatal"]) == ("error", "handshake", True)
    assert _no_tracebacks(caplog) == []


def test_text_that_is_not_json_is_a_typed_error(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello there")
        reply = ws.receive_json()
        assert (reply["type"], reply["code"]) == ("error", "handshake")


def test_handshake_with_no_keys_reports_each_missing_service(client):
    reply = _handshake(client, {"type": "config", "session_id": "abcdefgh12"})

    assert reply["type"] == "error"
    assert reply["code"] == "keys"
    assert reply["fatal"] is True
    for name in ("Deepgram", "Ollama", "Murf"):
        assert name in reply["message"]


# --- session ids --------------------------------------------------------------


def test_an_invalid_session_id_is_replaced_not_logged(
    client, caplog, keys_present, seen_session_ids
):
    caplog.set_level(logging.DEBUG)
    raw = "a" * 4000 + "\n" + "b" * 999

    reply = _handshake(client, {"type": "config", "session_id": raw})

    assert reply["code"] == "stt"  # got past validation, stopped at the fake Deepgram
    assert raw not in caplog.text
    assert "a" * 100 not in caplog.text
    assert max(len(line) for line in caplog.text.splitlines()) < 300
    assert seen_session_ids and valid_session_id(seen_session_ids[0])


def test_a_valid_session_id_is_kept_so_the_url_link_still_works(
    client, keys_present, seen_session_ids
):
    _handshake(client, {"type": "config", "session_id": "my-session_01"})

    assert seen_session_ids == ["my-session_01"]


def test_two_clients_without_an_id_do_not_share_a_conversation(
    client, keys_present, seen_session_ids
):
    _handshake(client, {"type": "config"})
    _handshake(client, {"type": "config"})

    first, second = seen_session_ids
    assert first != second
    assert "anonymous" not in (first, second)
    assert valid_session_id(first) and valid_session_id(second)


def test_history_routes_reject_malformed_ids(client):
    # A real conversation under a malformed id: if the routes did not validate,
    # GET would return it and DELETE would remove it.
    sessions.get("ab").add("user", "secret")
    try:
        before = len(sessions)

        assert client.get("/api/history/ab").json() == {"history": []}
        assert client.delete("/api/history/ab").json() == {"cleared": False}
        assert client.get("/api/history/nope-nope").json() == {"history": []}

        assert sessions.peek("ab") is not None, "DELETE must not touch the store"
        assert len(sessions) == before, "GET must not create"
    finally:
        sessions.clear("ab")


def test_history_routes_still_serve_valid_ids(client):
    sessions.get("history-ok-1").add("user", "hi")
    try:
        body = client.get("/api/history/history-ok-1").json()
        assert [turn["content"] for turn in body["history"]] == ["hi"]
        assert client.delete("/api/history/history-ok-1").json() == {"cleared": True}
    finally:
        sessions.clear("history-ok-1")


def test_valid_session_id_rules():
    assert valid_session_id("abcdefgh")
    assert valid_session_id("a" * 64)
    assert valid_session_id("3f2c9d1e-aaaa-bbbb-cccc-0123456789ab")
    assert valid_session_id("under_score-dash")
    assert not valid_session_id("short")
    assert not valid_session_id("a" * 65)
    assert not valid_session_id("has space 123")
    assert not valid_session_id("abcdefgh\n")  # `$` would let this through
    assert not valid_session_id("../../etc/passwd")
    assert not valid_session_id("")
    assert not valid_session_id(None)
    assert not valid_session_id(12345678)


# --- origin -------------------------------------------------------------------


def test_origin_check(client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/ws", headers={"origin": "https://evil.example"}
        ):
            pass
    assert excinfo.value.code == 1008

    # No Origin header (non-browser client) and a same-host Origin both get in.
    for headers in ({}, {"origin": "http://testserver"}):
        reply = _handshake(client, {"type": "config"}, headers)
        assert reply["code"] == "keys"


def test_origin_allowed_unit():
    assert origin_allowed(None, "example.com", set())
    assert origin_allowed("https://example.com", "example.com", set())
    assert origin_allowed("http://localhost:8000", "localhost:8000", set())
    assert origin_allowed("HTTPS://Example.COM", "example.com", set())
    assert not origin_allowed("https://evil.example", "example.com", set())
    assert not origin_allowed("http://localhost:9999", "localhost:8000", set())
    assert not origin_allowed("https://example.com.evil.example", "example.com", set())
    assert not origin_allowed("https://evil@example.com", "example.com", set())
    # A literal "null" origin has no host; it must not match an empty Host header.
    assert not origin_allowed("null", "", set())
    assert not origin_allowed("", "example.com", set())


def test_origin_allowed_honours_the_extra_list():
    extra = {"https://app.example.org"}
    assert origin_allowed("https://app.example.org", "example.com", extra)
    # Browsers send Origin in lowercase; the configured entry may not be.
    assert origin_allowed("https://app.example.org", "example.com", {"https://App.Example.org/"})
    assert origin_allowed("https://App.Example.org", "example.com", extra)
    assert not origin_allowed("https://other.example.org", "example.com", extra)


# --- HTTP ---------------------------------------------------------------------


def test_head_requests_succeed(client):
    assert client.head("/").status_code == 200
    assert client.head("/health").status_code == 200


def test_get_routes_still_work(client):
    assert client.get("/health").json()["status"] == "ok"
    assert "Meraki" in client.get("/").text


def test_index_does_not_walk_the_static_tree_per_request(client, monkeypatch):
    calls = []
    tokens = iter(["old", "new"])
    now = [1000.0]

    def counted():
        calls.append(1)
        return next(tokens)

    monkeypatch.setattr(main, "_compute_asset_version", counted)
    monkeypatch.setattr(main, "_asset_v_cache", None)
    monkeypatch.setattr(main, "_now", lambda: now[0])

    # (a) within the TTL the walk happens once, however many requests arrive.
    first = client.get("/").text
    now[0] += main.ASSET_VERSION_TTL / 2
    second = client.get("/").text
    assert len(calls) == 1
    assert "v=old" in first and "v=old" in second

    # (b) past it, the token is recomputed, so an edited file shows up even
    # though no process restart happened.
    now[0] += main.ASSET_VERSION_TTL
    third = client.get("/").text
    assert len(calls) == 2
    assert "v=new" in third


# --- keys ---------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["abc", ["x"], 7, None])
def test_api_keys_tolerate_a_non_dict_payload(payload):
    assert ApiKeys.from_payload(payload).missing() == ["Deepgram", "Ollama", "Murf"]
