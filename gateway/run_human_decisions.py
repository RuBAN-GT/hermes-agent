"""Session-bound human decisions requested by native plugins."""

import asyncio
import concurrent.futures
import dataclasses
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class GatewayHumanDecisionsMixin:
    def _schedule_plugin_human_decision(self, **kwargs):
        """Schedule a plugin decision request on this gateway's event loop."""
        from hermes_cli.human_decisions import error

        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or loop.is_closed():
            future = concurrent.futures.Future()
            future.set_result(error("gateway_unavailable"))
            return future
        from gateway.run import safe_schedule_threadsafe
        coro = self._dispatch_plugin_human_decision(**kwargs)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return loop.create_task(coro)
        future = safe_schedule_threadsafe(
            coro,
            loop,
            logger=logger,
            log_message="Plugin human-decision scheduling failed",
            log_level=logging.WARNING,
        )
        if future is None:
            failed = concurrent.futures.Future()
            failed.set_result(error("gateway_unavailable"))
            return failed
        return future

    async def _dispatch_plugin_human_decision(
        self,
        *,
        plugin_id: str,
        owner_id: str,
        title: str,
        body: str,
        choices,
        session_key: str,
        timeout_s: float,
    ):
        """Bind a decision to one current Telegram session before rendering it."""
        from gateway.config import Platform
        from hermes_cli.human_decisions import error, human_decisions

        if not getattr(self, "_running", False) or getattr(self, "_draining", False):
            return error("gateway_unavailable")
        entry = await self.async_session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None:
            return error("stale_session")
        source = dataclasses.replace(entry.origin)
        if source.platform != Platform.TELEGRAM:
            return error(
                "unsupported_platform", "human decisions currently require Telegram"
            )
        if not source.user_id:
            return error("no_session_actor")
        try:
            authorized = self._is_user_authorized(
                source,
                allow_adapter_delegation=False,
            )
        except Exception:
            logger.warning(
                "Plugin human-decision authorization check failed: plugin=%s session=%s",
                plugin_id,
                session_key,
                exc_info=True,
            )
            return error("unauthorized_actor")
        if not authorized:
            return error("unauthorized_actor")
        adapter = self._adapter_for_source(source)
        if adapter is None or not callable(
            getattr(adapter, "send_human_decision", None)
        ):
            return error(
                "unsupported_platform", "Telegram decision rendering is unavailable"
            )
        request = human_decisions.create(
            plugin_id=plugin_id,
            owner_id=owner_id,
            gateway_id=str(id(self)),
            title=title,
            body=body,
            choices=choices,
            session_key=session_key,
            session_id=entry.session_id,
            actor_id=str(source.user_id),
            chat_id=str(source.chat_id),
            thread_id=str(source.thread_id) if source.thread_id is not None else None,
            timeout_s=timeout_s,
        )
        if isinstance(request, dict):
            return request
        # Cleanup covers cancellation during the session re-read, send, and wait.
        try:
            current = await self.async_session_store.lookup_by_session_key(session_key)
            if current is None or current.session_id != request.session_id:
                return error("stale_session")
            remaining = request.expires_at - time.monotonic()
            if remaining <= 0:
                return error("timeout")
            sent = await asyncio.wait_for(
                adapter.send_human_decision(
                    str(source.chat_id), request,
                    metadata={"thread_id": request.thread_id},
                ), remaining,
            )
            if not getattr(sent, "success", False):
                return error("delivery_failed")
            return await human_decisions.wait(request.request_id)
        except asyncio.TimeoutError:
            return error("timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Plugin human-decision send failed: plugin=%s session=%s",
                plugin_id, session_key, exc_info=True,
            )
            return error("delivery_failed")
        finally:
            human_decisions.cancel(request.request_id, "cancelled")
            human_decisions.discard(request.request_id)

    async def _resolve_plugin_human_decision(
        self,
        *,
        token: str,
        choice_index: int,
        actor_id: str,
        chat_id: str,
        thread_id: Optional[str],
        adapter,
    ):
        """Validate current session ownership, then atomically consume a ticket."""
        from hermes_cli.human_decisions import error, human_decisions

        request = human_decisions.get_by_token(token)
        if request is None or request.gateway_id != str(id(self)):
            return error("stale")
        entry = await self.async_session_store.lookup_by_session_key(request.session_key)
        if entry is None or entry.origin is None or entry.session_id != request.session_id:
            human_decisions.cancel(request.request_id, "stale_session")
            return error("stale_session")
        source = dataclasses.replace(entry.origin)
        if self._adapter_for_source(source) is not adapter:
            return error("stale")
        source_thread = str(source.thread_id) if source.thread_id is not None else None
        if (
            str(source.user_id or "") != request.actor_id
            or str(source.chat_id) != request.chat_id
            or source_thread != request.thread_id
            or str(actor_id) != request.actor_id
            or str(chat_id) != request.chat_id
            or (str(thread_id) if thread_id is not None else None) != request.thread_id
        ):
            return error("unauthorized_actor")
        try:
            if not self._is_user_authorized(source, allow_adapter_delegation=False):
                return error("unauthorized_actor")
        except Exception:
            return error("unauthorized_actor")
        current = await self.async_session_store.lookup_by_session_key(
            request.session_key
        )
        if current is None or current.session_id != request.session_id:
            human_decisions.cancel(request.request_id, "stale_session")
            return error("stale_session")
        return human_decisions.resolve(
            token,
            choice_index,
            actor_id,
            chat_id,
            thread_id,
            current.session_id,
            str(id(self)),
        )
