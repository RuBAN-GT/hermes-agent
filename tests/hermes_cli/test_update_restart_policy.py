"""Disabled update restarts leave running services and pending recovery untouched."""

import json
from unittest.mock import Mock

import pytest


def test_disabled_restart_skips_fleet_catchup_cleanup_and_verification(tmp_path, monkeypatch):
    from hermes_cli import update_cmd, update_cmd_fleet as fleet, update_cmd_maint as maint
    from hermes_cli import update_receipt
    from hermes_cli.update_policy import restart_gateways_enabled

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("updates:\n  restart_gateways: false\n")
    assert restart_gateways_enabled() is False
    forbidden = Mock(side_effect=AssertionError("must not touch services"))
    monkeypatch.setattr(update_cmd, "_purge_stale_hermes_modules", forbidden)
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", forbidden)
    monkeypatch.setattr(update_cmd, "_reload_process_scan_modules", forbidden)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", forbidden)
    marker = tmp_path / "pending"
    marker.write_text("pending")
    monkeypatch.setattr(fleet, "_fleet_restart_pending_marker_path", lambda: marker)
    update_receipt.begin_update_receipt()
    outcome = fleet._restart_gateway_fleet_after_update(None, False)
    assert outcome.deferred
    assert not outcome.killed_pids and not outcome.restarted_services
    fleet._apply_pending_fleet_restart_catchup()
    assert fleet._run_pending_fleet_restart() is False
    maint._finish_dashboard_update_cleanup([])
    fleet._verify_fleet_after_update(
        outcome, _pre_update_plan=None, _windows_gateway_resume=None,
        node_failures=[], update_complete=True,
    )
    forbidden.assert_not_called()
    assert marker.read_text() == "pending"
    receipt = json.loads((tmp_path / "logs" / "update_receipts" / "latest.json").read_text())
    assert any(row["name"] == "gateway_restart" for row in receipt["skips"])
    assert "deferred" in receipt["stop_reason"]


def test_restart_policy_default_and_explicit_values(tmp_path, monkeypatch):
    from hermes_cli.update_policy import restart_gateways_enabled

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    for raw, expected in (("{}", True), ("updates: {restart_gateways: true}", True),
                          ("updates: {restart_gateways: false}", False)):
        path.write_text(raw)
        assert restart_gateways_enabled() is expected
    path.write_text('updates: {restart_gateways: "false"}')
    with pytest.raises(ValueError, match="true or false"):
        restart_gateways_enabled()


@pytest.mark.windows_only
def test_disabled_restart_never_pauses_or_cold_starts_windows_gateways(tmp_path, monkeypatch):
    from hermes_cli import update_cmd_windows as windows

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("updates: {restart_gateways: false}")
    forbidden = Mock(side_effect=AssertionError("must not stop or start gateways"))
    monkeypatch.setattr(windows, "_request_socket_pauses", forbidden)
    monkeypatch.setattr(windows, "_windows_cold_start_plan", forbidden)
    monkeypatch.setattr(windows, "_discover_windows_gateways", lambda: ({}, {}, set(), [42]))
    with pytest.raises(RuntimeError, match="stop Windows gateways manually"):
        windows._pause_windows_gateways_for_update()
    monkeypatch.setattr(windows, "_discover_windows_gateways", lambda: ({}, {}, set(), []))
    assert windows._pause_windows_gateways_for_update() is None
    forbidden.assert_not_called()
