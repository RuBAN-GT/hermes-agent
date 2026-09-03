"""Session-bound, one-shot human decisions for plugin gateway interactions.

This module owns no platform SDK objects. Gateway routing and rendering stay
host-owned; adapters only receive an opaque token and the validated request.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


MAX_CHOICES = 8
MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 3500
MAX_CHOICE_CHARS = 64
MAX_TIMEOUT_S = 3600


def error(code: str, detail: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": code}
    if detail:
        result["detail"] = detail
    return result


@dataclass(frozen=True)
class HumanDecisionRequest:
    request_id: str
    token: str
    plugin_id: str
    owner_id: str
    gateway_id: str
    title: str
    body: str
    choices: tuple[str, ...]
    session_key: str
    session_id: str
    actor_id: str
    chat_id: str
    thread_id: Optional[str]
    expires_at: float


@dataclass
class _Entry:
    request: HumanDecisionRequest
    future: Future


class HumanDecisions:
    """Process-local request store, safe to resolve from the gateway loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, _Entry] = {}
        self._by_token: dict[str, str] = {}
        self._completed: dict[str, Dict[str, Any]] = {}

    def create(
        self,
        *,
        plugin_id: str,
        owner_id: str,
        gateway_id: str,
        title: str,
        body: str,
        choices: Iterable[str],
        session_key: str,
        session_id: str,
        actor_id: str,
        chat_id: str,
        thread_id: Optional[str],
        timeout_s: float,
    ) -> HumanDecisionRequest | Dict[str, Any]:
        validated = self._validate(title, body, choices, timeout_s)
        if isinstance(validated, dict):
            return validated
        clean_choices, timeout = validated
        request = HumanDecisionRequest(
            request_id=uuid.uuid4().hex,
            # 22 URL-safe characters leave generous callback-data headroom.
            token=secrets.token_urlsafe(16),
            plugin_id=plugin_id,
            owner_id=owner_id,
            gateway_id=gateway_id,
            title=title.strip(),
            body=body.strip(),
            choices=clean_choices,
            session_key=session_key,
            session_id=session_id,
            actor_id=actor_id,
            chat_id=chat_id,
            thread_id=thread_id,
            expires_at=time.monotonic() + timeout,
        )
        with self._lock:
            self._by_id[request.request_id] = _Entry(request, Future())
            self._by_token[request.token] = request.request_id
        return request

    async def wait(self, request_id: str) -> Dict[str, Any]:
        with self._lock:
            completed = self._completed.pop(request_id, None)
            if completed is not None:
                return completed
            entry = self._by_id.get(request_id)
            if entry is None:
                return error("stale")
            remaining = entry.request.expires_at - time.monotonic()
            if remaining <= 0:
                self._finish_locked(entry, error("timeout"))
                return error("timeout")
            future = entry.future
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), remaining
            )
            with self._lock:
                self._completed.pop(request_id, None)
            return result
        except asyncio.TimeoutError:
            with self._lock:
                entry = self._by_id.get(request_id)
                if entry is not None:
                    self._finish_locked(entry, error("timeout"))
            return error("timeout")
        except asyncio.CancelledError:
            self.cancel(request_id, "cancelled")
            raise

    def get_by_token(self, token: str) -> Optional[HumanDecisionRequest]:
        """Return immutable request metadata for gateway-owned validation."""
        with self._lock:
            request_id = self._by_token.get(token)
            entry = self._by_id.get(request_id or "")
            return entry.request if entry is not None else None

    def resolve(
        self,
        token: str,
        choice_index: int,
        actor_id: str,
        chat_id: str,
        thread_id: Optional[str],
        session_id: str,
        gateway_id: str,
    ) -> Dict[str, Any]:
        """Consume a ticket once. Caller authorization is checked by the adapter too."""
        with self._lock:
            request_id = self._by_token.get(token)
            entry = self._by_id.get(request_id or "")
            if entry is None:
                return error("stale")
            request = entry.request
            if request.expires_at <= time.monotonic():
                self._finish_locked(entry, error("timeout"))
                return error("timeout")
            if str(actor_id) != request.actor_id:
                return error("unauthorized_actor")
            if str(chat_id) != request.chat_id:
                return error("stale")
            normalized_thread = str(thread_id) if thread_id is not None else None
            if normalized_thread != request.thread_id:
                return error("stale")
            if str(session_id) != request.session_id or gateway_id != request.gateway_id:
                return error("stale_session")
            if not isinstance(choice_index, int) or not 0 <= choice_index < len(request.choices):
                return error("invalid_choice")
            result = {
                "ok": True,
                "decision": request.choices[choice_index],
                "request_id": request.request_id,
                "actor_id": request.actor_id,
            }
            self._finish_locked(entry, result)
            return result

    def cancel(self, request_id: str, reason: str = "stale") -> None:
        with self._lock:
            entry = self._by_id.get(request_id)
            if entry is not None:
                self._finish_locked(entry, error(reason))

    def discard(self, request_id: str) -> None:
        """Drop a completed handoff when no caller will wait for it."""
        with self._lock:
            self._completed.pop(request_id, None)

    def cancel_owner(self, owner_id: str) -> None:
        """Cancel only one profile-scoped plugin registration owner."""
        with self._lock:
            for entry in tuple(self._by_id.values()):
                if entry.request.owner_id == owner_id:
                    self._finish_locked(entry, error("plugin_unloaded"))

    def cancel_gateway(self, gateway_id: str) -> None:
        """Wake every waiter owned by a gateway that is shutting down."""
        with self._lock:
            for entry in tuple(self._by_id.values()):
                if entry.request.gateway_id == gateway_id:
                    self._finish_locked(entry, error("gateway_unavailable"))

    def _finish_locked(self, entry: _Entry, result: Dict[str, Any]) -> None:
        request = entry.request
        self._by_id.pop(request.request_id, None)
        self._by_token.pop(request.token, None)
        self._completed[request.request_id] = result
        if not entry.future.done():
            entry.future.set_result(result)

    @staticmethod
    def _validate(
        title: Any, body: Any, choices: Iterable[str], timeout_s: Any,
    ) -> tuple[tuple[str, ...], float] | Dict[str, Any]:
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            return error("invalid_argument", "title must be 1-256 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
            return error("invalid_argument", "body must be 1-3500 characters")
        if isinstance(choices, str):
            return error("invalid_argument", "choices must be a sequence of strings")
        try:
            clean_choices = tuple(choice.strip() for choice in choices)
        except (TypeError, AttributeError):
            return error("invalid_argument", "choices must be a sequence of strings")
        if (
            not 2 <= len(clean_choices) <= MAX_CHOICES
            or any(not choice or len(choice) > MAX_CHOICE_CHARS for choice in clean_choices)
            or len(set(clean_choices)) != len(clean_choices)
        ):
            return error("invalid_argument", "choices must be 2-8 unique strings of at most 64 characters")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            return error("invalid_argument", "timeout_s must be a number")
        timeout = float(timeout_s)
        if not 1 <= timeout <= MAX_TIMEOUT_S:
            return error("invalid_argument", "timeout_s must be between 1 and 3600")
        return clean_choices, timeout


human_decisions = HumanDecisions()
