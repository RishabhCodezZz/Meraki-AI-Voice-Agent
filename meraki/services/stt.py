"""Deepgram Nova-3 streaming speech-to-text.

Contract (https://developers.deepgram.com/docs/live-streaming-audio):
    wss://api.deepgram.com/v1/listen?<params>
    Authorization: Token <DEEPGRAM_API_KEY>
    -> raw linear16 PCM frames as binary messages
    <- JSON, transcript at channel.alternatives[0].transcript

This is a plain WebSocket, so the whole module is ordinary asyncio. The previous
AssemblyAI implementation needed a worker thread, a blocking queue and a
thread-safe event bridge purely because its SDK was synchronous; none of that
exists any more.

Turn assembly follows Deepgram's two-flag model:
  is_final     - this segment is settled and will not be revised
  speech_final - endpointing fired; the speaker has finished a thought

Settled segments accumulate, and the utterance is emitted when speech_final
arrives. Using is_final alone would cut long sentences into fragments.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from ..config import DEEPGRAM_MODEL, SAMPLE_RATE

logger = logging.getLogger(__name__)

_WS_URL = "wss://api.deepgram.com/v1/listen"
_KEEPALIVE_INTERVAL = 8.0


def _params() -> dict[str, str]:
    return {
        "model": DEEPGRAM_MODEL,
        "language": "en",
        "encoding": "linear16",
        "sample_rate": str(SAMPLE_RATE),
        "channels": "1",
        "interim_results": "true",
        "punctuate": "true",
        "smart_format": "true",
        # Milliseconds of silence before Deepgram calls the thought finished.
        # Shorter feels snappy but clips people who pause mid-sentence.
        "endpointing": "350",
    }


class SpeechError(RuntimeError):
    pass


class SpeechStream:
    """One live transcription socket.

    Emits dicts onto ``events`` with a ``kind`` of ``open``, ``partial``,
    ``final``, ``error`` or ``closed``.
    """

    def __init__(self, http: aiohttp.ClientSession, api_key: str) -> None:
        self._http = http
        self._api_key = api_key
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader: Optional[asyncio.Task] = None
        self._keepalive: Optional[asyncio.Task] = None
        self._segments: list[str] = []
        self._closing = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        url = f"{_WS_URL}?{urlencode(_params())}"
        try:
            self._ws = await self._http.ws_connect(
                url,
                headers={"Authorization": f"Token {self._api_key}"},
                heartbeat=None,  # we send Deepgram's own KeepAlive instead
                max_msg_size=0,
            )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403):
                raise SpeechError("Deepgram rejected that API key.") from exc
            raise SpeechError(f"Deepgram refused the connection ({exc.status}).") from exc
        except aiohttp.ClientError as exc:
            raise SpeechError("Could not reach Deepgram.") from exc

        self._reader = asyncio.create_task(self._read_loop())
        self._keepalive = asyncio.create_task(self._keepalive_loop())
        await self.events.put({"kind": "open"})

    async def push(self, chunk: bytes) -> None:
        """Forward one microphone frame."""
        if self._ws is None or self._ws.closed or self._closing:
            return
        try:
            await self._ws.send_bytes(chunk)
        except (aiohttp.ClientError, ConnectionResetError):
            logger.debug("Dropped an audio frame; socket is going away")

    async def close(self) -> None:
        self._closing = True

        if self._keepalive is not None:
            self._keepalive.cancel()

        if self._ws is not None and not self._ws.closed:
            # Ask Deepgram to flush whatever it is still holding, then close.
            try:
                await self._ws.send_str(json.dumps({"type": "CloseStream"}))
            except (aiohttp.ClientError, ConnectionResetError):
                pass
            await self._ws.close()

        for task in (self._reader, self._keepalive):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must not raise
                logger.debug("Task raised during teardown", exc_info=True)

    # -- internals ----------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        """Deepgram closes an idle socket; this holds it open between turns."""
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            if self._ws is None or self._ws.closed:
                return
            try:
                await self._ws.send_str(json.dumps({"type": "KeepAlive"}))
            except (aiohttp.ClientError, ConnectionResetError):
                return

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._handle(message.json())
                elif message.type == aiohttp.WSMsgType.ERROR:
                    await self.events.put(
                        {"kind": "error", "message": "Deepgram connection error."}
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            if not self._closing:
                logger.exception("Deepgram read loop failed")
                await self.events.put({"kind": "error", "message": str(exc)})
        finally:
            await self.events.put({"kind": "closed"})

    async def _handle(self, payload: dict) -> None:
        if payload.get("type") == "Error" or "error" in payload:
            detail = payload.get("description") or payload.get("error") or "unknown"
            await self.events.put({"kind": "error", "message": str(detail)[:200]})
            return

        channel = payload.get("channel") or {}
        alternatives = channel.get("alternatives") or []
        if not alternatives:
            return

        transcript = (alternatives[0].get("transcript") or "").strip()
        is_final = bool(payload.get("is_final"))
        speech_final = bool(payload.get("speech_final"))

        if not is_final:
            if transcript:
                # Interim text, shown live but not yet acted on.
                await self.events.put(
                    {"kind": "partial", "text": self._joined(transcript)}
                )
            return

        if transcript:
            self._segments.append(transcript)

        if speech_final:
            utterance = " ".join(self._segments).strip()
            self._segments.clear()
            if utterance:
                await self.events.put({"kind": "final", "text": utterance})
        elif self._segments:
            await self.events.put({"kind": "partial", "text": self._joined("")})

    def _joined(self, tail: str) -> str:
        return " ".join([*self._segments, tail]).strip()
