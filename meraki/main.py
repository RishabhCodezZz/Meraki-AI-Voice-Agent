"""FastAPI application: HTTP routes and the voice WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import protocol
from .config import (
    ALLOWED_ORIGINS,
    APP_NAME,
    APP_TAGLINE,
    APP_VERSION,
    ApiKeys,
)
from .pipeline import TurnPipeline
from .security import origin_allowed
from .services.stt import SpeechError, SpeechStream
from .session import sessions, valid_session_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("meraki")


def _compute_asset_version() -> str:
    """Cache-busting token for /static, from the newest file's mtime.

    Without this the browser keeps serving the CSS and JS it already has, so a
    deploy ships new markup against old styles.
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


_asset_v_cache: Optional[str] = None


def _asset_version() -> str:
    """The token, computed once per process rather than walking /static per hit.

    Files only change between deploys, and a deploy restarts the process; under
    `--reload` the module reloads on any change, so development still sees it.
    """
    global _asset_v_cache
    if _asset_v_cache is None:
        _asset_v_cache = _compute_asset_version()
    return _asset_v_cache


# One connection pool shared by every request; created on startup.
_http: Optional[aiohttp.ClientSession] = None

# Barge-in only fires on a partial with at least this many words, so a stray
# syllable of echo does not cut the assistant off mid-sentence.
BARGE_IN_MIN_WORDS = 2

# A transcript needs at least this many words to count as echo. A lone "no" or
# "yes" is a plausible real answer, and one word is too little to be sure of.
ECHO_MIN_WORDS = 2

# How fast Murf's voice speaks, in characters of text per second. Used to guess
# when the audio will have finished playing; deliberately a little slow so the
# window errs towards ignoring echo a moment longer rather than a moment less.
ECHO_CHARS_PER_SECOND = 12.0

# Echo is still arriving this long after the speech ends: room reverb, the
# output buffer, and Deepgram's own latency.
ECHO_GRACE_SECONDS = 2.0

# Tests replace this to control the clock.
_now = time.monotonic

_WORDS = re.compile(r"[a-z0-9']+")


def _normalise(text: str) -> str:
    return " ".join(_WORDS.findall(text.lower()))


def looks_like_echo(heard: str, spoken: str) -> bool:
    """Is this the assistant hearing itself through the speakers?

    On speakers at volume the browser's echo cancellation is not enough, and
    Deepgram happily transcribes Meraki's own voice. Muting the microphone while
    it speaks would fix that by removing barge-in, which is the wrong trade.

    Instead: we know exactly what is being said, so a transcript contained in it
    is echo. Matching is on whole words, so "no" is not found inside "know", and
    needs at least ECHO_MIN_WORDS of them, so a bare "no" or "yes" always gets
    through. The caller stops asking once the reply has finished playing (see
    `_Connection._echo_active`), so an old reply cannot swallow a new answer.
    The remaining cost is that repeating 2+ words of it verbatim while it speaks
    will not interrupt it - rare, and recoverable by speaking again.
    """
    if not spoken:
        return False
    phrase = _normalise(heard)
    if len(phrase.split()) < ECHO_MIN_WORDS:
        return False
    # Pad both ends so the phrase can only match on word boundaries.
    return f" {phrase} " in f" {_normalise(spoken)} "


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http
    # The default connector caps at 100 sockets (one Deepgram socket per live
    # visitor, plus LLM and TTS calls) and has no connect timeout, so a black-holed
    # host would hang a handshake indefinitely. total=None leaves long streams
    # alone; llm.py and tts.py set their own per-read timeouts.
    _http = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=400),
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
    )
    logger.info("%s v%s ready", APP_NAME, APP_VERSION)
    # Say so at boot rather than letting the first visitor discover it.
    missing = ApiKeys.from_env().missing()
    if len(missing) == 3:
        # The intended posture for a public deploy, so this is not a warning.
        logger.info("No server keys set - every visitor brings their own.")
    elif missing:
        # Some but not all is almost always a misconfiguration: visitors will be
        # asked for keys they may assume are already provided.
        logger.warning("Partially configured - no server key for: %s", ", ".join(missing))
    yield
    await _http.close()


app = FastAPI(
    title=f"{APP_NAME} Voice Agent", version=APP_VERSION, lifespan=_lifespan
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# --- HTTP --------------------------------------------------------------------


# GET and HEAD both: load balancers and uptime checks probe with HEAD, and a 405
# there reads as the service being down.
@app.api_route("/", methods=["GET", "HEAD"])
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
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
    if not valid_session_id(session_id):
        return {"history": []}
    convo = sessions.peek(session_id)
    return {"history": [turn.as_dict() for turn in convo.turns] if convo else []}


@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str):
    if not valid_session_id(session_id):
        return {"cleared": False}
    return {"cleared": sessions.clear(session_id)}


@app.api_route("/health", methods=["GET", "HEAD"])
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
    # Before accept(), so a foreign page gets a refused upgrade, not a session.
    if not origin_allowed(
        websocket.headers.get("origin"),
        websocket.headers.get("host", ""),
        ALLOWED_ORIGINS,
    ):
        await websocket.close(code=1008)
        return
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
        # When that stops being worth checking against. Until the first audio
        # frame this is just the grace period; after it, the estimated end of
        # playback (see _echo_active).
        self._echo_deadline = float("-inf")
        self._first_audio_at: Optional[float] = None
        # An audio frame went out and no `interrupted` has since: the browser may
        # still be playing it even though the turn task finished long before.
        self._audio_pending = False
        # `interrupted` goes out once per utterance, however many partials follow.
        self._interrupt_sent = False

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
            frame = await asyncio.wait_for(self._ws.receive(), timeout=15)
            # receive_json() would raise KeyError on a binary frame, outside any
            # handler; take the raw frame and decode it ourselves.
            message = json.loads(frame["text"])
        except (asyncio.TimeoutError, ValueError, TypeError, KeyError):
            await self._send(
                protocol.error("handshake", "Expected a config message.", fatal=True)
            )
            return False

        # A JSON array or bare string parses fine but has no .get.
        if not isinstance(message, dict) or message.get("type") != "config":
            await self._send(
                protocol.error("handshake", "First message must be config.", fatal=True)
            )
            return False

        # Scoped to this connection. Never assign keys to module state.
        self._keys = ApiKeys.from_payload(message.get("keys") or {})
        if missing := self._keys.missing():
            names = ", ".join(missing)
            it = "it" if len(missing) == 1 else "them"
            await self._send(
                protocol.error(
                    "keys",
                    f"No key for {names}. Add {it} under Keys.",
                    fatal=True,
                )
            )
            return False

        # The browser's id is untrusted: keep it only if it is well formed, so
        # reloading still resumes the conversation, and otherwise mint one. Never
        # a shared fallback - everyone without an id would then share a history.
        requested = message.get("session_id")
        self._session_id = requested if valid_session_id(requested) else uuid.uuid4().hex
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

    async def _drain_speech_events(self) -> None:
        """Move STT events onto the socket and kick off turns.

        Wrapped because this task is the only thing feeding the conversation: if
        it dies the socket stays open, the browser keeps sending audio, and the
        UI sits on "Listening" forever with no clue anything is wrong.
        """
        assert self._speech is not None
        try:
            await self._pump_events()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - must reach the browser, not a log
            logger.exception("Speech event pump failed")
            await self._send(
                protocol.error("stt", "Lost the transcription stream.", fatal=True)
            )

    async def _pump_events(self) -> None:
        while True:
            event = await self._speech.events.get()
            kind = event.get("kind")

            if kind == "partial":
                text = event["text"]
                if looks_like_echo(text, self._spoken if self._echo_active() else ""):
                    logger.debug("Ignoring own voice: %r", text)
                    continue
                await self._send(protocol.partial(text))
                if len(text.split()) >= BARGE_IN_MIN_WORDS:
                    await self._cancel_turn(notify=True)

            elif kind == "final":
                text = event["text"]
                if looks_like_echo(text, self._spoken if self._echo_active() else ""):
                    logger.debug("Ignoring own voice (final): %r", text)
                    continue
                await self._send(protocol.final(text))
                # Notify here too: a one-word "stop" never reaches the partial
                # threshold, and the old reply may still be playing.
                await self._cancel_turn(notify=True)
                self._interrupt_sent = False  # the utterance is over
                self._turn = asyncio.create_task(self._run_turn(text))

            elif kind == "error":
                await self._send(protocol.error("stt", event.get("message", "")))

            elif kind == "closed":
                # Teardown cancels this task before closing the stream, so
                # reaching here means Deepgram went away on its own.
                logger.warning("Session %s lost its transcription stream", self._session_id)
                await self._send(
                    protocol.error(
                        "stt", "The transcription stream ended.", fatal=True
                    )
                )
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
        was_running = turn is not None and not turn.done()
        if was_running:
            turn.cancel()
            # Not `await turn` under suppress(CancelledError): that would also
            # swallow a cancel aimed at *us* while the turn unwinds, and a
            # close() waiting on this would never finish.
            await asyncio.wait({turn})
        if turn is not None and not turn.cancelled() and turn.exception():
            logger.error("Turn failed", exc_info=turn.exception())

        # The turn task ends when synthesis does, seconds before the browser has
        # played it out, so "nothing running" does not mean "nothing to cut off".
        # Audio that finished playing long ago is not worth announcing.
        audible = self._audio_pending and self._echo_active()
        if notify and (was_running or audible) and not self._interrupt_sent:
            await self._send(protocol.interrupted())
            self._interrupt_sent = True

    # -- outbound -----------------------------------------------------------

    def _echo_active(self) -> bool:
        """Could the microphone still be picking up the assistant's voice?

        `_spoken` used to linger until the next turn, so a phrase echoed from a
        reply finished minutes ago was dropped as the assistant "hearing itself".
        Once audio starts we know when it began and roughly how long the text
        takes to say; until then all we know is that a turn just started.
        """
        now = _now()
        if self._first_audio_at is None:
            return now < self._echo_deadline
        playback_ends = (
            self._first_audio_at
            + len(self._spoken) / ECHO_CHARS_PER_SECOND
            + ECHO_GRACE_SECONDS
        )
        return now < max(self._echo_deadline, playback_ends)

    async def _send(self, payload: dict) -> None:
        """Serialised send that tolerates a socket closing underneath us."""
        kind = payload.get("type")
        if kind == "thinking":
            self._spoken = ""
            self._first_audio_at = None
            self._echo_deadline = _now() + ECHO_GRACE_SECONDS
            # A new turn: audio from the last one is not this turn's to cut off,
            # and a silent turn must not inherit it as "still playing".
            self._audio_pending = False
        elif kind == "audio":
            self._audio_pending = True
            if self._first_audio_at is None:
                self._first_audio_at = _now()
        elif kind == "reply_chunk":
            # Held past the end of the turn on purpose: audio is still playing
            # out after the last token, and that tail echoes too.
            self._spoken += payload["text"]

        async with self._send_lock:
            try:
                await self._ws.send_json(payload)
            except (RuntimeError, WebSocketDisconnect):
                logger.debug("Send after close: %s", payload.get("type"))
        if kind == "interrupted":
            self._audio_pending = False

    # -- teardown -----------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._cancel_turn(notify=False)
            if self._pump is not None:
                self._pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump
            if self._speech is not None:
                try:
                    await self._speech.close()
                except Exception:  # noqa: BLE001 - teardown must reach ws.close()
                    logger.exception("Closing the transcription stream failed")
        finally:
            with contextlib.suppress(RuntimeError):
                await self._ws.close()
            logger.info("Session %s closed", self._session_id or "?")
