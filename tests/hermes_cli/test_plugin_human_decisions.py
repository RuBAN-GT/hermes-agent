"""Plugin facade capability and gateway-host contracts."""

import asyncio
from unittest.mock import MagicMock

import pytest
import yaml

from hermes_cli.human_decisions import human_decisions
from hermes_cli.plugin_human_decisions import PluginHumanDecisions
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _write_config(tmp_path, monkeypatch, entry):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {"plugin": entry}}})
    )
    monkeypatch.setenv("HERMES_HOME", str(home))


@pytest.mark.asyncio
async def test_ungranted_capability_never_calls_gateway(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {})
    manager = PluginManager()
    requester = MagicMock()
    manager.set_gateway_human_decisions(object(), requester)
    facade = PluginHumanDecisions("plugin", manager, lambda: False)

    result = await facade.request(
        title="Choose", body="Pick", choices=("yes", "no"), session_key="s",
    )

    assert result["error"] == "capability_not_granted"
    requester.assert_not_called()


@pytest.mark.asyncio
async def test_granted_capability_requires_session_key(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"allow_human_decisions": True})
    manager = PluginManager()
    facade = PluginHumanDecisions("plugin", manager, lambda: True)

    result = await facade.request(
        title="Choose", body="Pick", choices=("yes", "no"), session_key="",
    )

    assert result["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_plugin_unload_cancels_its_pending_decisions():
    with human_decisions._lock:
        human_decisions._by_id.clear()
        human_decisions._by_token.clear()
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="plugin", key="plugin"), manager)
    # Accessing the facade registers host-owned unload cleanup.
    context.human_decisions
    request = human_decisions.create(
        plugin_id="plugin",
        owner_id=f"{manager.scope_key}\0plugin",
        gateway_id="gateway-1",
        title="Choose",
        body="Pick one",
        choices=("yes", "no"),
        session_key="s",
        session_id="sid",
        actor_id="actor",
        chat_id="chat",
        thread_id=None,
        timeout_s=30,
    )
    assert not isinstance(request, dict)
    waiting = asyncio.create_task(human_decisions.wait(request.request_id))

    assert manager.unload("plugin") is True
    assert (await waiting)["error"] == "plugin_unloaded"


def test_capability_checks_stay_bound_to_plugin_owner_profile(tmp_path, monkeypatch):
    contexts = []
    for name, granted in (("allowed", True), ("denied", False)):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "plugins": {"entries": {"plugin": {"allow_human_decisions": granted}}}
        }))
        manager = PluginManager(scope_key=str(home))
        contexts.append(PluginContext(PluginManifest(name="plugin", key="plugin"), manager))
    for context in contexts:
        monkeypatch.setenv("HERMES_HOME", str(context._manager.home_path))
        assert contexts[0].has_capability("gateway.human_decisions")
        assert not contexts[1].has_capability("gateway.human_decisions")


def test_plugin_commands_preserve_legacy_and_session_aware_signatures():
    from hermes_cli.plugins import invoke_plugin_command_handler

    def legacy(raw_args):
        return raw_args

    def session_aware(raw_args, *, session_key):
        return raw_args, session_key

    def positional_only(raw_args, session_key="default", /):
        return raw_args, session_key

    assert invoke_plugin_command_handler(legacy, "go", session_key="s") == "go"
    assert invoke_plugin_command_handler(session_aware, "go", session_key="s") == ("go", "s")
    assert invoke_plugin_command_handler(positional_only, "go", session_key="s") == ("go", "default")
