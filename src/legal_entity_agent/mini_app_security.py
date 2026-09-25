"""Security primitives for the Telegram Mini App API.

The API is intentionally kept stateful inside the bot process.  A launch
context is an opaque, short-lived bearer value which maps to the chat where
the bot rendered the button.  The numeric chat id is never trusted from the
browser request body.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


class MiniAppSecurityError(ValueError):
    """A Mini App security token or replay marker is invalid."""


@dataclass(frozen=True, slots=True)
class MiniAppContext:
    """Server-side launch context bound to one Telegram chat."""

    chat_id: int
    chat_type: str
    issued_at: float
    expires_at: float


class MiniAppContextStore:
    """Creates short-lived opaque contexts and rejects repeated requests.

    Contexts deliberately live only in the bot process.  Restarting the bot
    invalidates all old Mini App links, which is a safe failure mode and also
    avoids adding another persistent secret-bearing database.
    """

    _MAX_TOKEN_LENGTH = 256
    _MAX_REQUEST_ID_LENGTH = 128

    def __init__(self, *, max_contexts: int = 4096) -> None:
        self._max_contexts = max(128, max_contexts)
        self._contexts: dict[str, MiniAppContext] = {}
        self._used_requests: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _safe_chat_type(chat_type: str | None) -> str:
        allowed = {"private", "group", "supergroup", "channel"}
        return chat_type if chat_type in allowed else "group"

    def _purge(self, now: float) -> None:
        expired_contexts = [
            token for token, context in self._contexts.items() if context.expires_at <= now
        ]
        for token in expired_contexts:
            self._contexts.pop(token, None)
        expired_requests = [
            key for key, expires_at in self._used_requests.items() if expires_at <= now
        ]
        for key in expired_requests:
            self._used_requests.pop(key, None)

    def issue(
        self,
        chat_id: int | str,
        *,
        chat_type: str | None = None,
        ttl_seconds: int = 900,
        now: float | None = None,
    ) -> str:
        try:
            numeric_chat_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise MiniAppSecurityError("Некорректный идентификатор чата.") from exc
        ttl = max(60, min(int(ttl_seconds), 3600))
        issued_at = self._now() if now is None else now
        token = secrets.token_urlsafe(32)
        context = MiniAppContext(
            chat_id=numeric_chat_id,
            chat_type=self._safe_chat_type(chat_type),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        with self._lock:
            self._purge(issued_at)
            while len(self._contexts) >= self._max_contexts:
                oldest_token = min(self._contexts, key=lambda item: self._contexts[item].expires_at)
                self._contexts.pop(oldest_token, None)
            self._contexts[token] = context
        return token

    def resolve(self, token: str, *, now: float | None = None) -> MiniAppContext:
        if not isinstance(token, str) or not token or len(token) > self._MAX_TOKEN_LENGTH:
            raise MiniAppSecurityError("Срок действия контекста Mini App истёк.")
        current = self._now() if now is None else now
        with self._lock:
            self._purge(current)
            context = self._contexts.get(token)
            if context is None or context.expires_at <= current:
                self._contexts.pop(token, None)
                raise MiniAppSecurityError("Срок действия контекста Mini App истёк.")
            return context

    def claim_request(self, token: str, request_id: str, *, now: float | None = None) -> bool:
        """Atomically accept one request id for a valid launch context."""

        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > self._MAX_REQUEST_ID_LENGTH
            or any(ord(character) < 33 or ord(character) > 126 for character in request_id)
        ):
            return False
        current = self._now() if now is None else now
        with self._lock:
            self._purge(current)
            context = self._contexts.get(token)
            if context is None or context.expires_at <= current:
                return False
            key = (token, request_id)
            if key in self._used_requests:
                return False
            self._used_requests[key] = context.expires_at
            return True

    def revoke_all(self) -> None:
        """Invalidate all currently issued links and replay markers."""

        with self._lock:
            self._contexts.clear()
            self._used_requests.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge(self._now())
            return len(self._contexts)


class MiniAppRateLimiter:
    """Small in-process sliding-window limiter for authenticated API calls."""

    def __init__(self, *, limit: int = 30, window_seconds: int = 60, max_keys: int = 8192) -> None:
        self.limit = max(1, min(int(limit), 1000))
        self.window_seconds = max(1, min(int(window_seconds), 3600))
        self.max_keys = max(128, max_keys)
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        if not isinstance(key, str) or not key or len(key) > 512:
            return False
        current = time.time() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(current)
            while len(self._events) > self.max_keys:
                oldest_key = min(self._events, key=lambda item: self._events[item][0] if self._events[item] else current)
                self._events.pop(oldest_key, None)
            return True

    def allow_all(self, *keys: str, now: float | None = None) -> bool:
        return all(self.allow(key, now=now) for key in keys)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
