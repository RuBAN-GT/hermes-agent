"""Telegram rendering and callback handling for plugin human decisions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from hermes_cli.human_decisions import human_decisions
from plugins.platforms.telegram.adapter import TelegramAdapter


def _clear_store():
    with human_decisions._lock:
        human_decisions._by_id.clear()
        human_decisions._by_token.clear()
        human_decisions._completed.clear()


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    return adapter


def _request():
    request = human_decisions.create(
        plugin_id="plugin",
        owner_id="owner:plugin",
        gateway_id="gateway-1",
        title="Choose <this>",
        body="Pick one",
        choices=("approve", "deny"),
        session_key="s",
        session_id="sid",
        actor_id="42",
        chat_id="42",
        thread_id=None,
        timeout_s=60,
    )
    assert not isinstance(request, dict)
    return request


@pytest.mark.asyncio
async def test_send_renders_short_callback_data_and_escaped_text():
    _clear_store()
    adapter = _adapter()
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=9)
    request = _request()

    result = await adapter.send_human_decision("42", request)

    assert result.success is True
    kwargs = adapter._bot.send_message.call_args.kwargs
    assert "&lt;this&gt;" in kwargs["text"]
    for row in kwargs["reply_markup"].inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64


def _query(data, actor="42", chat="42"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat
    query.message.chat.type = "private"
    query.message.message_thread_id = None
    query.from_user = MagicMock()
    query.from_user.id = actor
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _runner():
    async def resolve(**kwargs):
        request = human_decisions.get_by_token(kwargs["token"])
        assert request is not None
        return human_decisions.resolve(
            kwargs["token"],
            kwargs["choice_index"],
            kwargs["actor_id"],
            kwargs["chat_id"],
            kwargs["thread_id"],
            request.session_id,
            request.gateway_id,
        )

    return SimpleNamespace(_resolve_plugin_human_decision=resolve)


@pytest.mark.asyncio
async def test_callback_rejects_foreign_actor_without_consuming_ticket():
    _clear_store()
    adapter = _adapter()
    request = _request()
    query = _query(f"hd:{request.token}:0", actor="99")
    adapter._is_callback_user_authorized = MagicMock(return_value=True)

    with patch("gateway.run._gateway_runner_ref", return_value=_runner()):
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock())

    assert request.request_id in human_decisions._by_id
    assert "another user" in query.answer.call_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_callback_consumes_ticket_and_removes_keyboard():
    _clear_store()
    adapter = _adapter()
    request = _request()
    query = _query(f"hd:{request.token}:1")
    adapter._is_callback_user_authorized = MagicMock(return_value=True)

    with patch("gateway.run._gateway_runner_ref", return_value=_runner()):
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), MagicMock())

    assert request.request_id not in human_decisions._by_id
    query.edit_message_text.assert_called_once()
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_gateway_resolver_rejects_rotated_session():
    from gateway.run import GatewayRunner

    _clear_store()
    runner = object.__new__(GatewayRunner)
    origin = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="42",
    )
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        lookup_by_session_key=AsyncMock(
            return_value=SimpleNamespace(
                session_id="replacement-session",
                origin=origin,
            )
        )
    )
    request = human_decisions.create(
        plugin_id="plugin",
        owner_id="owner:plugin",
        gateway_id=str(id(runner)),
        title="Choose",
        body="Pick one",
        choices=("approve", "deny"),
        session_key="session-key",
        session_id="original-session",
        actor_id="42",
        chat_id="42",
        thread_id=None,
        timeout_s=60,
    )
    assert not isinstance(request, dict)

    result = await runner._resolve_plugin_human_decision(
        token=request.token,
        choice_index=0,
        actor_id="42",
        chat_id="42",
        thread_id=None,
    )

    assert result["error"] == "stale_session"
    assert request.request_id not in human_decisions._by_id
