"""One conversational turn: transcript in, streamed reply and speech out.

A turn is a single cancellable task. When the user starts talking over the
assistant the task is cancelled mid-flight, which unwinds the LLM request and
any in-progress synthesis, and whatever was said up to that point is still
recorded so the conversation stays coherent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Awaitable, Callable

import aiohttp

from . import protocol
from .config import ApiKeys
from .services import llm, tts
from .session import Conversation

logger = logging.getLogger(__name__)

Sender = Callable[[dict], Awaitable[None]]


class TurnPipeline:
    def __init__(
        self,
        send: Sender,
        http: aiohttp.ClientSession,
        keys: ApiKeys,
        conversation: Conversation,
        model: str,
        voice_id: str,
    ) -> None:
        self._send = send
        self._http = http
        self._keys = keys
        self._convo = conversation
        self._model = model
        self._voice_id = voice_id

    async def run(self, user_text: str) -> None:
        self._convo.add("user", user_text)
        history = self._convo.as_messages()[:-1]  # exclude the turn we just added
        reply_parts: list[str] = []

        try:
            await self._send(protocol.thinking())

            async def tee() -> AsyncGenerator[str, None]:
                """Forward tokens to the browser and on to synthesis."""
                async for token in llm.stream_reply(
                    self._http, self._keys.ollama, self._model, history, user_text
                ):
                    reply_parts.append(token)
                    await self._send(protocol.reply_chunk(token))
                    yield token

            seq = 0
            async for audio_b64 in tts.stream_speech(
                self._http, self._keys.murf, tee(), self._voice_id
            ):
                await self._send(protocol.audio(seq, audio_b64))
                seq += 1

            done = "".join(reply_parts).strip()
            if done:
                await self._send(protocol.reply_done(done))
            await self._send(protocol.speech_done())

        except llm.LLMError as exc:
            await self._send(protocol.error("llm", str(exc)))
        except tts.TTSError as exc:
            # The text is already on screen; only the audio failed.
            await self._send(protocol.error("tts", str(exc)))
            await self._send(protocol.speech_done())
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Upstream failure during turn: %s", exc)
            await self._send(
                protocol.error("network", "Lost contact with an upstream service.")
            )
        except Exception:  # noqa: BLE001 - never kill the socket over one turn
            logger.exception("Unhandled error during turn")
            await self._send(protocol.error("internal", "Something went wrong."))
        finally:
            # Runs on cancellation too, so an interrupted reply is still
            # remembered up to the point the user cut in.
            spoken = "".join(reply_parts).strip()
            if spoken:
                self._convo.add("assistant", spoken)
