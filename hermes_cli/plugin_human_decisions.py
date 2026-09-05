"""Capability-gated human-decision facade handed to native plugins."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any, Callable, Dict, Iterable

from hermes_cli.human_decisions import error, human_decisions

logger = logging.getLogger(__name__)

CAPABILITY_ID = "gateway.human_decisions"


class PluginHumanDecisions:
    """Request a decision in the caller's existing gateway session only."""

    def __init__(
        self,
        plugin_id: str,
        manager: Any,
        capability_check: Callable[[], bool],
    ) -> None:
        self._plugin_id = plugin_id
        self._manager = manager
        self._capability_check = capability_check
        self._owner_id = f"{manager.scope_key}\0{plugin_id}"

    async def request(
        self,
        *,
        title: str,
        body: str,
        choices: Iterable[str],
        session_key: str,
        timeout_s: float = 300,
    ) -> Dict[str, Any]:
        """Render a Telegram decision and await exactly one actor-bound choice."""
        if not self._capability_granted():
            return error("capability_not_granted")
        if not isinstance(session_key, str) or not session_key.strip():
            return error("invalid_argument", "session_key is required for gateway decisions")
        if not self._manager.has_gateway_human_decisions:
            return error("gateway_unavailable")
        try:
            pending = self._manager.request_gateway_human_decision(
                plugin_id=self._plugin_id,
                owner_id=self._owner_id,
                title=title,
                body=body,
                choices=choices,
                session_key=session_key,
                timeout_s=timeout_s,
            )
            if isinstance(pending, concurrent.futures.Future):
                return await asyncio.wrap_future(pending)
            if asyncio.isfuture(pending) or hasattr(pending, "__await__"):
                return await pending
            return error("gateway_unavailable")
        except Exception:
            logger.warning(
                "human_decisions request failed for plugin %s", self._plugin_id,
                exc_info=True,
            )
            return error("gateway_unavailable")

    def cancel_all(self) -> None:
        human_decisions.cancel_owner(self._owner_id)

    def _capability_granted(self) -> bool:
        try:
            return bool(self._capability_check())
        except Exception:
            return False
