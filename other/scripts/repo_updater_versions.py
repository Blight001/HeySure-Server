"""Git-history safety checks shared by the host repository updater."""

from __future__ import annotations

from typing import Any, Callable


DEFAULT_VERSION_LIMIT = 20
MAX_VERSION_LIMIT = 50
ROOT_SERVER_PATH = "deploy/server"
SERVER_MIGRATIONS_PATH = "other/migrations/versions"
SERVER_RELEASE_SCRIPT = "other/scripts/rolling_release.py"
DATA_ROOT = "/www/wwwroot/heysureai2/deploy/server/data"

GitOutput = Callable[[list[str], float], str]


class VersionSelectionError(RuntimeError):
    pass


def bounded_limit(limit: int) -> int:
    return max(1, min(MAX_VERSION_LIMIT, int(limit)))


def normalize_target_sha(target_sha: str) -> str:
    target = str(target_sha or "").strip().lower()
    if len(target) != 40 or any(char not in "0123456789abcdef" for char in target):
        raise VersionSelectionError("目标版本必须是完整的 40 位 commit SHA")
    return target


def _server_gitlink(git_output: GitOutput, root_ref: str) -> str:
    try:
        raw = git_output(["ls-tree", root_ref, "--", ROOT_SERVER_PATH], 15)
    except Exception:
        return ""
    metadata, separator, path = raw.partition("\t")
    fields = metadata.split()
    if separator and path == ROOT_SERVER_PATH and len(fields) == 3:
        mode, object_type, sha = fields
        if mode == "160000" and object_type == "commit" and len(sha) == 40:
            return sha.lower()
    return ""


def _migration_tree(
    git_output: GitOutput,
    server_git_output: GitOutput,
    root_ref: str,
) -> str:
    server_sha = _server_gitlink(git_output, root_ref)
    if not server_sha:
        return ""
    try:
        return server_git_output(
            ["rev-parse", f"{server_sha}:{SERVER_MIGRATIONS_PATH}"], 15
        ).lower()
    except Exception:
        return ""


def _server_file_exists(
    git_output: GitOutput,
    server_git_output: GitOutput,
    root_ref: str,
    path: str,
) -> bool:
    server_sha = _server_gitlink(git_output, root_ref)
    if not server_sha:
        return False
    try:
        server_git_output(["cat-file", "-e", f"{server_sha}:{path}"], 15)
        return True
    except Exception:
        return False


def safe_compose_definition(git_output: GitOutput, root_ref: str) -> bool:
    try:
        content = git_output(["show", f"{root_ref}:docker-compose.yml"], 15)
    except Exception:
        return False
    db_mount = f"{DATA_ROOT}:/var/lib/postgresql/data"
    runtime_mount = f"{DATA_ROOT}/app:/app/data"
    volume_lines = [line.strip().removeprefix("-").strip().strip("'\"") for line in content.splitlines()]
    db_lines = [line for line in volume_lines if line.endswith(":/var/lib/postgresql/data")]
    runtime_lines = [line for line in volume_lines if line.endswith(":/app/data")]
    return (
        db_lines == [db_mount]
        and runtime_lines == [runtime_mount] * 4
        and "PGDATA=/var/lib/postgresql/data/postgres" in content
        and f"{DATA_ROOT}/postgres:" not in content
    )


def version_history(
    git_output: GitOutput,
    server_git_output: GitOutput,
    limit: int = DEFAULT_VERSION_LIMIT,
) -> dict[str, Any]:
    bounded = bounded_limit(limit)
    current_sha = git_output(["rev-parse", "HEAD"], 15).lower()
    current_tree = _migration_tree(git_output, server_git_output, "HEAD")
    raw = git_output(
        [
            "log",
            "--first-parent",
            f"--max-count={bounded}",
            "--format=%H%x1f%h%x1f%an%x1f%ct%x1f%s%x1e",
            "HEAD",
        ],
        30,
    )
    versions: list[dict[str, Any]] = []
    for record in raw.split("\x1e"):
        fields = record.strip().split("\x1f", 4)
        if len(fields) != 5:
            continue
        sha, short, author, timestamp, subject = fields
        try:
            committed_at: float | None = float(timestamp)
        except ValueError:
            committed_at = None
        is_current = sha.lower() == current_sha
        compatible = bool(current_tree) and (
            _migration_tree(git_output, server_git_output, sha) == current_tree
        )
        has_release_script = _server_file_exists(
            git_output, server_git_output, sha, SERVER_RELEASE_SCRIPT
        )
        safe_compose = safe_compose_definition(git_output, sha)
        eligible = not is_current and compatible and has_release_script and safe_compose
        reason = None
        if is_current:
            reason = "当前版本"
        elif not compatible:
            reason = "数据库迁移版本不兼容，不能安全回退"
        elif not has_release_script:
            reason = "目标版本缺少安全滚动发布脚本"
        elif not safe_compose:
            reason = "目标版本 Compose 持久化路径不符合安全约束"
        versions.append(
            {
                "sha": sha,
                "short": short,
                "author": author,
                "committed_at": committed_at,
                "subject": subject,
                "is_current": is_current,
                "rollback_eligible": eligible,
                "disabled_reason": reason,
            }
        )
    return {
        "versions": versions,
        "current_sha": current_sha,
        "limit": bounded,
        "max_limit": MAX_VERSION_LIMIT,
    }


def rollback_candidate(
    git_output: GitOutput,
    server_git_output: GitOutput,
    target_sha: str,
) -> tuple[str, dict[str, Any]]:
    target = normalize_target_sha(target_sha)
    try:
        resolved = git_output(["rev-parse", "--verify", f"{target}^{{commit}}"], 15).lower()
    except Exception as exc:
        raise VersionSelectionError("目标版本不是可用的 commit") from exc
    if resolved != target:
        raise VersionSelectionError("目标版本不是可用的 commit")
    history = version_history(git_output, server_git_output, MAX_VERSION_LIMIT)
    for version in history["versions"]:
        if str(version.get("sha") or "").lower() != target:
            continue
        if version.get("is_current"):
            raise VersionSelectionError("目标版本就是当前版本，不能回退")
        if not version.get("rollback_eligible"):
            reason = str(version.get("disabled_reason") or "目标版本不能安全回退")
            raise VersionSelectionError(reason)
        return str(history["current_sha"]), dict(version)
    raise VersionSelectionError("目标版本不是当前 HEAD 的可回退 first-parent 祖先")
