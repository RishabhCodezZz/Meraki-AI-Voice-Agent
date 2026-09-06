"""Ollama Cloud streaming client.

Endpoint contract (https://docs.ollama.com/api/chat):
    POST https://ollama.com/api/chat
    Authorization: Bearer <OLLAMA_API_KEY>
    {"model": ..., "messages": [{"role","content"}], "stream": true}
  -> newline-delimited JSON; incremental text at ``message.content``,
     terminated by a chunk with ``done: true``.

``think`` is sent as false because the Nemotron models are reasoning models.
Reasoning tokens arrive before any speakable text, so on a voice agent they buy
nothing and cost seconds of silence.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

import aiohttp

from ..config import LLM_STALL_TIMEOUT, OLLAMA_CHAT_URL, SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


async def stream_reply(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    history: list[dict],
    user_text: str,
) -> AsyncGenerator[str, None]:
    """Yield reply text as it is generated.

    ``history`` is prior turns as ``{"role", "content"}``; ``user_text`` is the
    new message and is not expected to be present in history.
    """
    if not api_key:
        raise LLMError("Ollama API key is missing.")

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + list(history)
        + [{"role": "user", "content": user_text}]
    )

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0.8,
            "top_p": 0.9,
            "num_predict": 220,
        },
    }

    # No total cap: a slow-but-progressing stream should not be killed. The
    # stall timeout catches an upstream that has actually gone quiet.
    timeout = aiohttp.ClientTimeout(total=None, sock_read=LLM_STALL_TIMEOUT)

    async with session.post(
        OLLAMA_CHAT_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    ) as response:
        if response.status != 200:
            detail = (await response.text())[:400]
            logger.error("Ollama %s: %s", response.status, detail)
            raise LLMError(_explain(response.status, model, detail))

        async for raw_line in response.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Unparseable NDJSON line: %s", line[:120])
                continue

            if error := event.get("error"):
                raise LLMError(str(error)[:200])

            text = (event.get("message") or {}).get("content")
            if text:
                yield text

            if event.get("done"):
                break


def _explain(status: int, model: str, detail: str) -> str:
    if status in (401, 403):
        return "Ollama rejected that API key."
    if status == 404:
        return f"Model '{model}' is not available on your Ollama account."
    if status == 429:
        return "Ollama rate limit reached - the free tier has hourly caps."
    if status >= 500:
        return "Ollama Cloud is having trouble right now."
    return f"Ollama returned {status}."
