"""Real repositories exercise repeatable fork updates and transactional conflicts."""

import subprocess

import pytest


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo, name, content):
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    from hermes_cli import main, update_cmd_git

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    for key, value in {"GIT_AUTHOR_NAME": "Updater test", "GIT_AUTHOR_EMAIL": "test@example.com",
                       "GIT_COMMITTER_NAME": "Updater test", "GIT_COMMITTER_EMAIL": "test@example.com"}.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("updates: {restart_gateways: false}\n")
    official, origin, server = (tmp_path / name for name in ("official", "origin", "server"))
    official.mkdir()
    git(official, "init", "-b", "main")
    commit(official, "shared.txt", "base\n")
    git(tmp_path, "clone", str(official), str(origin))
    git(origin, "checkout", "-b", "custom")
    commit(origin, "custom.txt", "our changes\n")
    git(tmp_path, "clone", "-b", "custom", str(origin), str(server))
    monkeypatch.setattr(main, "PROJECT_ROOT", server)
    monkeypatch.setattr(update_cmd_git, "OFFICIAL_REPO_URL", str(official))
    return official, origin, server


def prepare(server):
    from hermes_cli.update_cmd import _prepare_checkout_for_update
    from hermes_cli.update_cmd_custom import select_fork_branch

    branch, merge = select_fork_branch(
        ["git"], "main", is_fork=True, explicit_branch=False, switch_branch=False)
    assert (branch, merge) == ("custom", True)
    git(server, "fetch", "origin", branch)
    return _prepare_checkout_for_update(
        ["git"], branch, "custom", is_fork=True, assume_yes=True, gateway_mode=False,
        gw_input_fn=None, switch_branch=False, _windows_gateway_resume=None,
        merge_upstream=merge,
    )


def pull(plan):
    from hermes_cli.update_cmd import _pull_updates

    return _pull_updates(
        ["git"], "custom", plan.auto_stash_ref, prompt_for_restore=False,
        gw_input_fn=None, discard_local_changes=False, keep_stash=False,
        upstream_ref=plan.upstream_ref,
    )


def test_updates_preserve_local_history_and_edits_across_repeated_runs(repos):
    from hermes_cli.update_cmd import _verify_head_after_pull
    from hermes_cli.update_policy import restart_gateways_enabled

    official, origin, server = repos
    git(server, "config", "merge.ff", "only")
    local = commit(server, "server.txt", "local commit\n")
    for round_ in range(2):
        upstream_tip = commit(official, f"upstream-{round_}.txt", "upstream\n")
        fork_tip = commit(origin, f"fork-{round_}.txt", "fork\n")
        (server / "custom.txt").write_text("operator edits\n")
        plan = prepare(server)
        assert plan.commit_count > 0 and plan.upstream_checked
        before = pull(plan)
        after = _verify_head_after_pull(
            ["git"], "custom", before, in_place_update=False, _windows_gateway_resume=None)
        assert before != after
        for sha in (local, upstream_tip, fork_tip):
            git(server, "merge-base", "--is-ancestor", sha, "HEAD")
        assert (server / "custom.txt").read_text() == "operator edits\n"
        assert git(origin, "rev-parse", "HEAD") == fork_tip  # no automatic publication
        current = prepare(server)
        assert current.commit_count == 0
        pull(current)  # settle the real autostash without changing HEAD
        assert git(server, "rev-parse", "HEAD") == after
        assert restart_gateways_enabled() is False


@pytest.mark.parametrize("failure", ["origin", "official", "fetch"])
def test_failed_update_keeps_history_and_edits_without_leaving_merge(repos, failure, capsys, monkeypatch):
    from hermes_cli import update_cmd_git

    official, origin, server = repos
    before = commit(server, "shared.txt", "local\n")
    commit(origin, "new-fork.txt", "new\n")
    if failure == "fetch":
        monkeypatch.setattr(update_cmd_git, "OFFICIAL_REPO_URL", str(official / "missing"))
    else:
        commit(origin if failure == "origin" else official, "shared.txt", "conflict\n")
    (server / "custom.txt").write_text("uncommitted operator edits\n")
    with pytest.raises(SystemExit) as error:
        pull(prepare(server))
    assert error.value.code == 1
    assert git(server, "rev-parse", "HEAD") == before
    assert not (server / ".git" / "MERGE_HEAD").exists()
    assert not (server / "new-fork.txt").exists()
    git(server, "stash", "apply")
    assert (server / "custom.txt").read_text() == "uncommitted operator edits\n"
    output = capsys.readouterr().out
    assert "Update stopped" in output
    assert "Code updated!" not in output and "Already up to date!" not in output
