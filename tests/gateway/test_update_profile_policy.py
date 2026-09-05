"""Telegram update denial uses the receiving profile's loaded adapter config."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.asyncio
async def test_telegram_update_policy_is_profile_scoped(tmp_path, monkeypatch):
    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.platforms.base import MessageEvent

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    for profile, setting in (("blocked", "false"), ("allowed", "true")):
        home = tmp_path / profile
        home.mkdir()
        (home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n"
            f"        allow_update_command: {setting}\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        config = load_gateway_config()
        runner._profile_adapters[profile] = {
            Platform.TELEGRAM: SimpleNamespace(config=config.platforms[Platform.TELEGRAM])
        }
    # Stop at managed-install check on allowed profiles, before any spawn/write.
    managed = Mock(return_value=True)
    monkeypatch.setattr("hermes_cli.config.is_managed", managed)
    for profile in ("blocked", "allowed", "blocked"):
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", profile=profile)
        response = await runner._handle_update_command(MessageEvent(text="/update", source=source))
        assert ("disabled for this profile" in response) is (profile == "blocked")
    assert managed.call_count == 1
