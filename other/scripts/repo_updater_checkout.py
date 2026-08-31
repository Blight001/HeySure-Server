"""Clean-worktree and deployment-submodule operations for the host updater."""

from __future__ import annotations

import subprocess
from typing import Callable, Type


DEPLOY_SUBMODULES = ("deploy/server", "deploy/web")
Git = Callable[[list[str], float], subprocess.CompletedProcess[str]]
GitOutput = Callable[[list[str], float], str]


def ensure_clean_worktree(git: Git, error_type: Type[RuntimeError]) -> None:
    # Device is an independent deployment surface and is intentionally not
    # part of the server Compose release.  A developer checkout on the host
    # may therefore have platform submodule changes that must not block a
    # server-only update or incorrectly poison deployment consistency.
    status = git(
        [
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            "--ignore-submodules=none",
            "--",
            ":(exclude)device",
        ],
        30,
    )
    if status.stdout.strip():
        raise error_type("repository or submodules contain uncommitted changes")


def sync_deployment_submodules(
    git: Git,
    git_output: GitOutput,
    error_type: Type[RuntimeError],
) -> None:
    paths = list(DEPLOY_SUBMODULES)
    git(["submodule", "sync", "--recursive", "--", *paths], 120)
    git(["submodule", "update", "--init", "--recursive", "--force", "--", *paths], 300)
    verify_deployment_checkout(git, git_output, error_type)


def verify_deployment_checkout(
    git: Git,
    git_output: GitOutput,
    error_type: Type[RuntimeError],
) -> None:
    paths = list(DEPLOY_SUBMODULES)
    top_level = git_output(["submodule", "status", "--", *paths], 30).splitlines()
    recursive = git_output(["submodule", "status", "--recursive", "--", *paths], 60)
    if len(top_level) != len(paths) or any(not line.startswith(" ") for line in top_level):
        raise error_type("required deployment submodule checkout does not match gitlink")
    if any(not line.startswith(" ") for line in recursive.splitlines()):
        raise error_type("nested deployment submodule checkout does not match gitlink")
    ensure_clean_worktree(git, error_type)


def fast_forward_to(git: Git, upstream: str) -> None:
    git(["merge", "--ff-only", upstream], 300)


def restore_checkout(
    git: Git,
    git_output: GitOutput,
    error_type: Type[RuntimeError],
    commit_sha: str,
) -> None:
    git(["reset", "--hard", commit_sha], 120)
    sync_deployment_submodules(git, git_output, error_type)
