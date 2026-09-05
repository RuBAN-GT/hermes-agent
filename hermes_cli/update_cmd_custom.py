"""Merge fork and official updates into a custom checkout without publishing it."""

import sys


def select_fork_branch(git_cmd, branch, *, is_fork, explicit_branch, switch_branch):
    from hermes_cli.update_cmd import _current_branch_name

    if not is_fork or switch_branch:
        return branch, False
    current = _current_branch_name(git_cmd, check=True)
    if current in {"", "HEAD", "main", "master"}:
        return branch, False
    if explicit_branch and branch != current:
        return branch, False
    print(f"→ Updating custom branch '{current}' from origin/{current} and official main.")
    return current, True


def fetch_official_main(git_cmd):
    from hermes_cli.update_cmd import _git_run
    from hermes_cli.update_cmd_git import OFFICIAL_REPO_URL

    # Pin the fetched commit; a configured upstream remote need not be the official repo.
    print("→ Fetching official main...")
    result = _git_run(git_cmd, ["fetch", "--no-tags", OFFICIAL_REPO_URL, "main"], network=True)
    if result.returncode:
        print(f"✗ Failed to fetch official main. Update stopped.\n{result.stderr.strip()}")
        sys.exit(1)
    return _git_run(git_cmd, ["rev-parse", "FETCH_HEAD"], check=True).stdout.strip()


def merge_fork_updates(git_cmd, branch, upstream_ref):
    from hermes_cli.update_cmd import _git_run

    if _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]).returncode == 0:
        print("✗ A merge is already in progress. Finish or abort it before updating.")
        sys.exit(1)
    before = _git_run(git_cmd, ["rev-parse", "HEAD"], check=True).stdout.strip()
    for ref, label in ((f"origin/{branch}", f"origin/{branch}"), (upstream_ref, "official main")):
        print(f"→ Merging {label}...")
        result = _git_run(git_cmd, ["merge", "--ff", "--no-edit", ref])
        if result.returncode == 0:
            continue
        conflicts = _git_run(git_cmd, ["diff", "--name-only", "--diff-filter=U"]).stdout.strip()
        print(f"✗ Could not merge {label}. Update stopped.")
        if conflicts:
            print(f"  Conflicting files:\n{conflicts}")
        else:
            print(result.stderr.strip() or result.stdout.strip())
        # The caller parked local edits. Abort only our merge, then undo any earlier
        # successful merge in this update so dependencies still match the checkout.
        if _git_run(git_cmd, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]).returncode == 0:
            if _git_run(git_cmd, ["merge", "--abort"]).returncode != 0:
                print("  Could not abort merge. Resolve it manually before retrying.")
                sys.exit(1)
        if _git_run(git_cmd, ["reset", "--hard", before]).returncode:
            print(f"  Could not restore checkout. Recovery commit: {before}")
        else:
            print("  Restored the checkout to its pre-update commit. No changes were pushed.")
        sys.exit(1)
