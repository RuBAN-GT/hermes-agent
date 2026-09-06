"""execute_code and terminal must create the same configured Docker environment."""

from types import SimpleNamespace


def test_execute_code_preserves_terminal_container_policy(tmp_path, monkeypatch):
    from tools import code_execution_tool, terminal_tool, terminal_tool_backends
    from tools.terminal_scope import install_and_reset_profile_terminal_scope

    (tmp_path / "config.yaml").write_text("""
terminal:
  backend: docker
  docker_forward_env: [REVIEW_SERVICE_TOKEN]
  docker_env: {REVIEW_REGION: test-region}
  docker_mount_cwd_to_workspace: true
  docker_extra_args: [--init]
  docker_network: false
""")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_maybe_reap_docker_orphans", lambda _: None)
    constructed = []

    def docker(**kwargs):
        constructed.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(terminal_tool_backends, "_DockerEnvironment", docker)
    with install_and_reset_profile_terminal_scope(tmp_path):
        config = terminal_tool._get_env_config()
        code_execution_tool._get_or_create_env("review-docker-policy")
        terminal_tool_backends._create_environment(
            env_type="docker", image=constructed[0]["image"], cwd=config["cwd"],
            timeout=config["timeout"], task_id="review-docker-policy",
            container_config=terminal_tool_backends._container_config_from_config(config),
        )
    actual, expected = constructed
    for key in ("forward_env", "env", "auto_mount_cwd", "extra_args", "network"):
        assert actual[key] == expected[key]
    assert actual["forward_env"] == ["REVIEW_SERVICE_TOKEN"]
    assert actual["env"] == {"REVIEW_REGION": "test-region"}
    assert actual["network"] is False
