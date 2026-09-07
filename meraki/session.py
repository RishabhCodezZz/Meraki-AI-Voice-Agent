"""In-memory conversation store.

History is keyed by the *client-supplied* session id so a page reload or a
dropped WebSocket resumes the same conversation. Entries are held in an LRU
with a TTL so a long-running process cannot grow without bound.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

from .config import MAX_HISTORY_MESSAGES, MAX_SESSIONS, SESSION_TTL_SECONDS


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "ts": self.ts}


@dataclass
class Conversation:
    turns: list[Turn] = field(default_factory=list)
    touched: float = field(default_factory=time.time)

    def add(self, role: str, content: str) -> None:
        self.turns.append(Turn(role, content))
        del self.turns[:-MAX_HISTORY_MESSAGES]
        self.touched = time.time()

    def as_messages(self) -> list[dict]:
        """Ollama chat format: ``{"role": "user"|"assistant", "content": str}``."""
        return [
            {"role": turn.role, "content": turn.content} for turn in self.turns
        ]


class SessionStore:
    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        ttl: float = SESSION_TTL_SECONDS,
    ) -> None:
        self._data: OrderedDict[str, Conversation] = OrderedDict()
        self._max = max_sessions
        self._ttl = ttl
        self._lock = Lock()

    def get(self, session_id: str) -> Conversation:
        with self._lock:
            convo = self._data.get(session_id)
            if convo is None:
                convo = Conversation()
                self._data[session_id] = convo
            self._data.move_to_end(session_id)
            # Evict after inserting, so the cap holds including the new entry.
            self._evict(protect=session_id)
            return convo

    def peek(self, session_id: str) -> Optional[Conversation]:
        """Read without creating.

        A GET must not mutate the store. It used to, which meant requesting
        unknown session ids conjured empty conversations and evicted real ones
        straight out of the LRU.
        """
        with self._lock:
            convo = self._data.get(session_id)
            if convo is not None:
                self._data.move_to_end(session_id)
            return convo

    def clear(self, session_id: str) -> bool:
        with self._lock:
            return self._data.pop(session_id, None) is not None

    def _evict(self, protect: str | None = None) -> None:
        """Caller must hold the lock."""
        cutoff = time.time() - self._ttl
        stale = [
            key
            for key, convo in self._data.items()
            if convo.touched < cutoff and key != protect
        ]
        for key in stale:
            del self._data[key]
        while len(self._data) > self._max:
            oldest, _ = next(iter(self._data.items()))
            if oldest == protect:
                break
            del self._data[oldest]

    def __len__(self) -> int:  # pragma: no cover - diagnostics only
        with self._lock:
            return len(self._data)


sessions = SessionStore()
