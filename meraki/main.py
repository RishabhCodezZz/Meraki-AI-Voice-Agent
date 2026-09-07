"""FastAPI application: HTTP routes and the voice WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import protocol
from .config import (
    APP_NAME,
    APP_TAGLINE,
    APP_VERSION,
    ApiKeys,
)
from .pipeline import TurnPipeline
from .services.stt import SpeechError, SpeechStream
from .session import sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("meraki")


def _asset_version() -> str:
    """Cache-busting token for /static, from the newest file's mtime.

    Without this the browser keeps serving the CSS and JS it already has, so a
    deploy ships new markup against old styles. Changes on every restart, which
    is also what you want while developing.
    """
    try:
        newest = max(
            path.stat().st_mtime
            for path in Path("static").rglob("*")
            if path.is_file()
        )
    except (OSError, ValueError):
        return APP_VERSION
    return f"{int(newest):x}"

# One connection pool shared by every request; created on startup.
_http: Optional[aiohttp.ClientSession] = None

# Barge-in only fires on a partial with at least this many words, so a stray
# syllable of echo does not cut the assistant off mid-sentence.
BARGE_IN_MIN_WORDS = 2

_WORDS = re.compile(r"[a-z0-9']+")


def _normalise(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


def looks_like_echo(heard: str, spoken: str) -> bool:
    """Is this the assistant hearing itself through the speakers?

    On speakers at volume the browser's echo cancellation is not enough, and
    Deepgram happily transcribes Meraki's own voice. Muting the microphone while
    it speaks would fix that by removing barge-in, which is the wrong trade.

    Instead: we know exactly what is being said, so a transcript contained in it
    is echo. The cost is that saying a phrase back verbatim while it is speaking
    will not interrupt it - rare, and recoverable by speaking again.
    """
    if not spoken:
        return False
    phrase = _normalise(heard)
    return bool(phrase) and phrase in _normalise(spoken)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http
    _http = aiohttp.ClientSession()
    logger.info("%s v%s ready", APP_NAME, APP_VERSION)
    # Say so at boot rather than letting the first visitor discover it.
    if missing := ApiKeys.from_env().missing():
        logger.warning(
            "No server key for: %s. Visitors must supply their own in Settings.",
            ", ".join(missing),
        )
    yield
    await _http.close()


app = FastAPI(
    title=f"{APP_NAME} Voice Agent", version=APP_VERSION, lifespan=_lifespan
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# --- HTTP --------------------------------------------------------------------


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "tagline": APP_TAGLINE,
            "asset_v": _asset_version(),
            # Lets the page decide whether to demand keys up front.
            "keys_required": bool(ApiKeys.from_env().missing()),
            "version": APP_VERSION,
        },
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    return {"history": [turn.as_dict() for turn in sessions.get(session_id).turns]}


@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str):
    return {"cleared": sessions.clear(session_id)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "sessions": len(sessions),
    }


# --- WebSocket ---------------------------------------------------------------


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    connection = _Connection(websocket)
    try:
        await connection.run()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket handler crashed")
    finally:
        await connection.close()


class _Connection:
    """State for a single browser connection.

    Credentials live here and nowhere else — they are never written to module
    or application state, so concurrent visitors cannot see each other's keys.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._speech: Optional[SpeechStream] = None
        self._turn: Optional[asyncio.Task] = None
        self._pump: Optional[asyncio.Task] = None
        self._keys: Optional[ApiKeys] = None
        self._session_id = ""
        self._send_lock = asyncio.Lock()
        # What the assistant is currently saying, used to recognise its own
        # voice coming back through the microphone.
        self._spoken = ""

    async def run(self) -> None:
        if not await self._handshake():
            return

        self._speech = SpeechStream(_http, self._keys.deepgram)
        try:
            await self._speech.start()
        except SpeechError as exc:
            await self._send(protocol.error("stt", str(exc), fatal=True))
            return

        self._pump = asyncio.create_task(self._drain_speech_events())
        await self._send(protocol.ready())
        logger.info("Session %s live", self._session_id)

        await self._receive_loop()

    # -- setup --------------------------------------------------------------

    async def _handshake(self) -> bool:
        """Wait for the opening config frame carrying keys and session id."""
        try:
            message = await asyncio.wait_for(self._ws.receive_json(), timeout=15)
        except (asyncio.TimeoutError, ValueError):
            await self._send(
                protocol.error("handshake", "Expected a config message.", fatal=True)
            )
            return False

        if message.get("type") != "config":
            await self._send(
                protocol.error("handshake", "First message must be config.", fatal=True)
            )
            return False

        # Scoped to this connection. Never assign keys to module state.
        self._keys = ApiKeys.from_payload(message.get("keys") or {})
        if missing := self._keys.missing():
            await self._send(
                protocol.error(
                    "keys",
                    f"Missing {', '.join(missing)}. Add it under Settings.",
                    fatal=True,
                )
            )
            return False

        self._session_id = str(message.get("session_id") or "").strip() or "anonymous"
        # Model and voice are server-side settings. Anything the browser sends
        # for them is ignored on purpose.
        logger.info("Session %s configured", self._session_id)
        return True

    # -- inbound ------------------------------------------------------------

    async def _receive_loop(self) -> None:
        while True:
            message = await self._ws.receive()

            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))

            if (data := message.get("bytes")) is not None:
                await self._speech.push(data)
                continue

            text = message.get("text")
            if not text:
                continue
            if text == "stop":
                logger.info("Session %s stopped recording", self._session_id)
                break
            if text == "interrupt":
                await self._cancel_turn(notify=True)

    async def _drain_speech_events(self) -> None:
        """Move STT events onto the socket and kick off turns."""
        assert self._speech is not None
        while True:
            event = await self._speech.events.get()
            kind = event.get("kind")

            if kind == "partial":
                text = event["text"]
                if looks_like_echo(text, self._spoken):
                    logger.debug("Ignoring own voice: %r", text)
                    continue
                await self._send(protocol.partial(text))
                if len(text.split()) >= BARGE_IN_MIN_WORDS:
                    await self._cancel_turn(notify=True)

            elif kind == "final":
                text = event["text"]
                if looks_like_echo(text, self._spoken):
                    logger.debug("Ignoring own voice (final): %r", text)
                    continue
                await self._send(protocol.final(text))
                await self._cancel_turn(notify=False)
                self._turn = asyncio.create_task(self._run_turn(text))

            elif kind == "error":
                await self._send(protocol.error("stt", event.get("message", "")))

            elif kind == "closed":
                break

    # -- turns --------------------------------------------------------------

    async def _run_turn(self, text: str) -> None:
        pipeline = TurnPipeline(
            send=self._send,
            http=_http,
            keys=self._keys,
            conversation=sessions.get(self._session_id),
        )
        await pipeline.run(text)

    async def _cancel_turn(self, *, notify: bool) -> None:
        turn, self._turn = self._turn, None
        if turn is None or turn.done():
            return
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        if notify:
            await self._send(protocol.interrupted())

    # -- outbound -----------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        """Serialised send that tolerates a socket closing underneath us."""
        kind = payload.get("type")
        if kind == "thinking":
            self._spoken = ""
        elif kind == "reply_chunk":
            # Held past the end of the turn on purpose: audio is still playing
            # out after the last token, and that tail echoes too.
            self._spoken += payload["text"]

        async with self._send_lock:
            try:
                await self._ws.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                logger.debug("Send after close: %s", payload.get("type"))

    # -- teardown -----------------------------------------------------------

    async def close(self) -> None:
        await self._cancel_turn(notify=False)
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        if self._speech is not None:
            await self._speech.close()
        with contextlib.suppress(RuntimeError):
            await self._ws.close()
        logger.info("Session %s closed", self._session_id or "?")
