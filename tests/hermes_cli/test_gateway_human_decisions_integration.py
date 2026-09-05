"""Real plugin discovery, session storage, Telegram SDK, and callback routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")
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


@pytest.mark.asyncio
async def test_discovered_plugin_command_decides_through_real_session_and_callback(tmp_path, monkeypatch):
    import asyncio
    import weakref
    import yaml
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.platforms.base import MessageEvent
    from hermes_cli.plugins import PluginManager

    _clear_store()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["decision-probe"], "entries": {
            "decision-probe": {"granted_capabilities": ["gateway.human_decisions"]}
        }}
    }))
    plugin_dir = tmp_path / "plugins" / "decision-probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: decision-probe\nversion: 1.0.0\ncapabilities: [gateway.human_decisions]\n"
    )
    (plugin_dir / "__init__.py").write_text("""
def register(ctx):
    async def decide(raw_args, *, session_key):
        result = await ctx.human_decisions.request(
            title="Approve task", body=raw_args, choices=("yes", "no"),
            session_key=session_key,
        )
        return result.get("decision", result.get("error"))
    ctx.register_command("decision-probe", decide)
""")
    config = GatewayConfig()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42")
    entry = store.get_or_create_session(source)
    adapter = _adapter()
    sent = asyncio.Event()
    prompts = []

    async def send(**kwargs):
        prompts.append(kwargs)
        sent.set()
        return SimpleNamespace(message_id=9)

    adapter._bot.send_message.side_effect = send
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    manager = PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr("gateway.run._gateway_runner_ref", weakref.ref(runner))
    runner._install_plugin_message_injector()
    event = MessageEvent(text="/decision-probe Publish draft", source=source)
    task = asyncio.create_task(runner._hm_dispatch_quick_and_plugin_commands(
        event, source, "decision-probe",
    ))
    try:
        await asyncio.wait_for(sent.wait(), 5)
        data = prompts[0]["reply_markup"].inline_keyboard[0][0].callback_data
        assert isinstance(data, str), repr(data)
        query = _query(data, actor="99")
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
        assert not task.done()
        # Even the right actor cannot redeem the token through a different bot.
        foreign_adapter = _adapter()
        query = _query(data)
        await foreign_adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
        assert not task.done()
        query = _query(data)
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
        assert query.answer.call_args.kwargs["text"] == "✓ yes"
        assert await asyncio.wait_for(task, 5) == (True, "yes", "decision-probe")
        assert not human_decisions._by_id
        assert not human_decisions._completed
        replay = _query(data)
        await adapter._handle_callback_query(SimpleNamespace(callback_query=replay), None)
        replay.edit_message_text.assert_not_called()
        assert store.lookup_by_session_key(entry.session_key).session_id == entry.session_id
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        runner._clear_plugin_message_injector()
        manager.unload()
