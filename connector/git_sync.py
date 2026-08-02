"""
Handles committing trade/equity data back to the repo so the dashboard
(hosted via GitHub Pages) can pick it up. Reuses the fetch/rebase retry
pattern that solved concurrent push conflicts in the Binance bots — the
connector runs in a loop and GitHub Actions journal jobs may also commit,
so pushes can collide.
"""

import subprocess
import time
from connector.config import REPO_PATH, GIT_PUSH_RETRY_ATTEMPTS, GIT_PUSH_RETRY_DELAY_SEC


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_PATH, capture_output=True, text=True)


def commit_and_push(paths: list[str], message: str) -> bool:
    """
    Stages the given file paths (relative to repo root), commits, and pushes.
    Retries with fetch + rebase on conflict, same pattern as the trading-bot repo.
    Returns True on success, False if all retries exhausted.
    """
    _run(["git", "add", *paths])

    diff = _run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return True  # nothing changed, nothing to push

    commit = _run(["git", "commit", "-m", message])
    if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
        print(f"[git_sync] commit failed: {commit.stderr}")
        return False

    for attempt in range(1, GIT_PUSH_RETRY_ATTEMPTS + 1):
        push = _run(["git", "push"])
        if push.returncode == 0:
            return True

        print(f"[git_sync] push failed (attempt {attempt}/{GIT_PUSH_RETRY_ATTEMPTS}), "
              f"fetching + rebasing: {push.stderr.strip()}")
        _run(["git", "fetch", "origin"])
        rebase = _run(["git", "rebase", "origin/main"])
        if rebase.returncode != 0:
            print(f"[git_sync] rebase failed, aborting rebase: {rebase.stderr.strip()}")
            _run(["git", "rebase", "--abort"])
            time.sleep(GIT_PUSH_RETRY_DELAY_SEC)
            continue

        time.sleep(GIT_PUSH_RETRY_DELAY_SEC)

    print("[git_sync] giving up after all retries — data stayed local, will retry next cycle")
    return False
