"""Telegram presentation for host-owned plugin human decisions."""

import html as _html
import logging
from typing import Any, Dict, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


class TelegramHumanDecisionsMixin:
    async def send_human_decision(
        self,
        chat_id: str,
        request,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a host-validated plugin decision without exposing Telegram SDKs."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        from gateway.platforms.base import utf16_len
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id
        from plugins.platforms.telegram.adapter import _redact_telegram_error_text
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            escaped_title = _html.escape(request.title)
            escaped_body = _html.escape(request.body)
            text = f"<b>{escaped_title}</b>\n\n{escaped_body}"
            if utf16_len(text) > 4000:
                low, high = 0, len(request.body)
                while low < high:
                    mid = (low + high + 1) // 2
                    candidate = (
                        f"<b>{escaped_title}</b>\n\n"
                        f"{_html.escape(request.body[:mid])}…"
                    )
                    if utf16_len(candidate) <= 4000:
                        low = mid
                    else:
                        high = mid - 1
                text = (
                    f"<b>{escaped_title}</b>\n\n"
                    f"{_html.escape(request.body[:low])}…"
                )
            rows = [
                [
                    InlineKeyboardButton(
                        choice,
                        callback_data=f"hd:{request.token}:{index}",
                    )
                ]
                for index, choice in enumerate(request.choices)
            ]
            keyboard = InlineKeyboardMarkup(rows)
            thread_id = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(
                None, metadata, reply_to_mode=self._reply_to_mode,
            )
            msg = await self._send_message_with_thread_fallback(
                chat_id=normalize_telegram_chat_id(chat_id),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                reply_to_message_id=reply_to_id,
                **self._thread_kwargs_for_send(
                    chat_id,
                    thread_id,
                    metadata,
                    reply_to_message_id=reply_to_id,
                    reply_to_mode=self._reply_to_mode,
                ),
                **self._link_preview_kwargs(),
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as exc:
            logger.warning(
                "[%s] send_human_decision failed: %s",
                self.name, _redact_telegram_error_text(exc),
            )
            return SendResult(success=False, error=_redact_telegram_error_text(exc))


    async def _handle_human_decision_callback(self, query, data, cb):
        from telegram.constants import ParseMode
        query_chat_id = cb["chat_id"]
        query_chat_type = cb["chat_type"]
        query_thread_id = cb["thread_id"]
        query_user_name = cb["user_name"]
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer(text="Invalid decision data.")
            return
        token, raw_index = parts[1], parts[2]
        try:
            choice_index = int(raw_index)
        except ValueError:
            await query.answer(text="Invalid decision data.")
            return
        caller_id = str(getattr(query.from_user, "id", ""))
        if not self._is_callback_user_authorized(
            caller_id,
            chat_id=query_chat_id,
            chat_type=str(query_chat_type) if query_chat_type is not None else None,
            thread_id=str(query_thread_id) if query_thread_id is not None else None,
            user_name=query_user_name,
        ):
            await query.answer(text="⛔ You are not authorized to answer this prompt.")
            return
        try:
            from gateway.run import _gateway_runner_ref

            runner = _gateway_runner_ref()
            resolver = getattr(runner, "_resolve_plugin_human_decision", None)
            if not callable(resolver):
                result = {"ok": False, "error": "stale"}
            else:
                result = await resolver(
                    adapter=self,
                    token=token,
                    choice_index=choice_index,
                    actor_id=caller_id,
                    chat_id=str(query_chat_id or ""),
                    thread_id=(
                        str(query_thread_id)
                        if query_thread_id is not None
                        else None
                    ),
                )
        except Exception:
            logger.warning(
                "[%s] human-decision callback validation failed",
                self.name,
                exc_info=True,
            )
            result = {"ok": False, "error": "stale"}
        if not result.get("ok"):
            labels = {
                "unauthorized_actor": "⛔ This prompt belongs to another user.",
                "timeout": "⌛ This prompt expired.",
                "stale": "⌛ This prompt was already resolved.",
                "stale_session": "⌛ This session has changed.",
            }
            await query.answer(text=labels.get(result.get("error"), "This prompt is unavailable."))
            return
        decision = str(result["decision"])
        user_display = _html.escape(getattr(query.from_user, "first_name", "User"))
        await query.answer(text=f"✓ {decision[:60]}")
        try:
            await query.edit_message_text(
                text=f"<b>Decision:</b> {_html.escape(decision)} by {user_display}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            pass
        return
