"""Operator policy for update-driven service restarts."""


def restart_gateways_enabled() -> bool:
    from hermes_cli.config import load_config_readonly

    section = (load_config_readonly() or {}).get("updates", {})
    if not isinstance(section, dict):
        raise ValueError("updates must be a mapping")
    enabled = section.get("restart_gateways", True)
    if not isinstance(enabled, bool):
        raise ValueError("updates.restart_gateways must be true or false")
    return enabled


def report_deferred_gateway_restart() -> None:
    from hermes_cli.update_receipt import record_skip

    print("→ Automatic service restart disabled (updates.restart_gateways: false).")
    print("  Running gateways keep their old code; restart each profile manually when ready.")
    record_skip("gateway_restart", "updates.restart_gateways=false; manual restart required")
