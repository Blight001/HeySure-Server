"""Version history and safe rollback orchestration for repository updates."""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, List

from sqlmodel import Session

from api.core.settings import SERVER_DIR
from api.services import repo_update


DEFAULT_VERSION_LIMIT = 20
MAX_VERSION_LIMIT = 50
ROLLBACK_WARNING = "回退后已自动关闭自动更新；数据库迁移不会自动降级。"
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_ROLLBACK_PHASES = {"queued_rollback", "backing_up", "rolling_back"}
_ROOT_SERVER_PATHS = ("deploy/server", "server")
_SERVER_MIGRATIONS_PATH = "other/migrations/versions"
_SERVER_RELEASE_SCRIPT = "other/scripts/rolling_release.py"
_DATA_ROOT = "/www/wwwroot/heysureai2/deploy/server/data"


class RepoRollbackError(repo_update.RepoUpdateError):
    """The selected commit cannot be rolled back safely."""


class RepoRollbackBusy(RepoRollbackError):
    """Another repository operation already owns the updater."""


def _bounded_limit(limit: int) -> int:
    return max(1, min(MAX_VERSION_LIMIT, int(limit)))


def _normalize_target_sha(target_sha: str) -> str:
    value = str(target_sha or "").strip().lower()
    if not _FULL_SHA.fullmatch(value):
        raise RepoRollbackError("目标版本必须是完整的 40 位 commit SHA")
    return value


def _parse_history(raw: str, current_sha: str) -> List[Dict[str, Any]]:
    versions: List[Dict[str, Any]] = []
    for record in raw.split("\x1e"):
        fields = record.strip().split("\x1f", 4)
        if len(fields) != 5:
            continue
        sha, short, author, timestamp, subject = fields
        try:
            committed_at = float(timestamp)
        except (TypeError, ValueError):
            committed_at = None
        is_current = sha.lower() == current_sha.lower()
        versions.append(
            {
                "sha": sha,
                "short": short,
                "author": author,
                "committed_at": committed_at,
                "subject": subject,
                "is_current": is_current,
                "rollback_eligible": not is_current,
                "disabled_reason": "当前版本" if is_current else None,
            }
        )
    return versions


def _server_gitlink(ref: str) -> str:
    for path in _ROOT_SERVER_PATHS:
        result = repo_update._run_git(["ls-tree", ref, "--", path], timeout=15)
        metadata, separator, actual_path = result.stdout.strip().partition("\t")
        fields = metadata.split()
        if separator and actual_path == path and len(fields) == 3:
            mode, object_type, sha = fields
            if mode == "160000" and object_type == "commit" and len(sha) == 40:
                return sha.lower()
    return ""


def _migration_tree(ref: str) -> str:
    server_sha = _server_gitlink(ref)
    if not server_sha:
        return ""
    result = repo_update._run_git(
        ["-C", SERVER_DIR, "rev-parse", f"{server_sha}:{_SERVER_MIGRATIONS_PATH}"],
        timeout=15,
    )
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def _server_file_exists(ref: str, path: str) -> bool:
    server_sha = _server_gitlink(ref)
    if not server_sha:
        return False
    result = repo_update._run_git(
        ["-C", SERVER_DIR, "cat-file", "-e", f"{server_sha}:{path}"], timeout=15
    )
    return result.returncode == 0


def _safe_compose_definition(ref: str) -> bool:
    result = repo_update._run_git(["show", f"{ref}:docker-compose.yml"], timeout=15)
    if result.returncode != 0:
        return False
    db_mount = f"{_DATA_ROOT}:/var/lib/postgresql/data"
    runtime_mount = f"{_DATA_ROOT}/app:/app/data"
    volume_lines = [
        line.strip().removeprefix("-").strip().strip("'\"")
        for line in result.stdout.splitlines()
    ]
    db_lines = [line for line in volume_lines if line.endswith(":/var/lib/postgresql/data")]
    runtime_lines = [line for line in volume_lines if line.endswith(":/app/data")]
    return (
        db_lines == [db_mount]
        and runtime_lines == [runtime_mount] * 4
        and "PGDATA=/var/lib/postgresql/data/postgres" in result.stdout
        and f"{_DATA_ROOT}/postgres:" not in result.stdout
    )


def _mark_migration_compatibility(versions: List[Dict[str, Any]]) -> None:
    current_tree = _migration_tree("HEAD")
    for version in versions:
        if version["is_current"]:
            continue
        target_tree = _migration_tree(str(version["sha"]))
        if not current_tree or target_tree != current_tree:
            version["rollback_eligible"] = False
            version["disabled_reason"] = "数据库迁移版本不兼容，不能安全回退"
            continue
        if not _server_file_exists(str(version["sha"]), _SERVER_RELEASE_SCRIPT):
            version["rollback_eligible"] = False
            version["disabled_reason"] = "目标版本缺少安全滚动发布脚本"
        elif not _safe_compose_definition(str(version["sha"])):
            version["rollback_eligible"] = False
            version["disabled_reason"] = "目标版本 Compose 持久化路径不符合安全约束"


def _local_version_history(limit: int) -> Dict[str, Any]:
    if not repo_update.git_available():
        raise RepoRollbackError("当前部署不是可用的 git 工作区")
    head = repo_update._run_git(["rev-parse", "HEAD"], timeout=15)
    if head.returncode != 0 or not head.stdout.strip():
        raise RepoRollbackError("无法读取当前版本")
    current_sha = head.stdout.strip().lower()
    history = repo_update._run_git(
        [
            "log",
            "--first-parent",
            f"--max-count={_bounded_limit(limit)}",
            "--format=%H%x1f%h%x1f%an%x1f%ct%x1f%s%x1e",
            "HEAD",
        ],
        timeout=30,
    )
    if history.returncode != 0:
        raise RepoRollbackError("无法读取版本历史")
    versions = _parse_history(history.stdout, current_sha)
    _mark_migration_compatibility(versions)
    for version in versions:
        if not version["is_current"]:
            version["rollback_eligible"] = False
            version["disabled_reason"] = "当前部署缺少宿主安全回退执行器"
    return _history_payload(versions, current_sha, limit)


def _history_payload(versions: List[Dict[str, Any]], current_sha: str, limit: int) -> Dict[str, Any]:
    return {
        "versions": versions,
        "current_sha": current_sha or None,
        "limit": _bounded_limit(limit),
        "max_limit": MAX_VERSION_LIMIT,
        "rollback_warning": ROLLBACK_WARNING,
    }


def list_versions(limit: int = DEFAULT_VERSION_LIMIT) -> Dict[str, Any]:
    """Return HEAD and its first-parent history from the active updater."""
    bounded = _bounded_limit(limit)
    mode = repo_update.update_mode()
    if mode == "remote":
        payload = repo_update._remote_request("GET", f"/versions?limit={bounded}", timeout=15)
        versions = payload.get("versions")
        if not isinstance(versions, list):
            raise RepoRollbackError("宿主更新服务返回了无效的版本历史")
        payload.update(
            limit=bounded,
            max_limit=MAX_VERSION_LIMIT,
            rollback_warning=ROLLBACK_WARNING,
            update_mode="remote",
        )
        return payload
    if mode == "git":
        payload = _local_version_history(bounded)
        payload["update_mode"] = "git"
        return payload
    raise RepoRollbackError("未连接宿主更新服务，且当前部署不是可用的 git 工作区")


def _selected_version(target_sha: str) -> tuple[Dict[str, Any], str]:
    target = _normalize_target_sha(target_sha)
    history = list_versions(MAX_VERSION_LIMIT)
    for version in history["versions"]:
        if str(version.get("sha") or "").lower() != target:
            continue
        if not version.get("rollback_eligible"):
            if version.get("is_current"):
                raise RepoRollbackError("目标版本就是当前版本，不能回退")
            raise RepoRollbackError(str(version.get("disabled_reason") or "目标版本不能安全回退"))
        return dict(version), str(history.get("update_mode") or "")
    raise RepoRollbackError("目标版本不是当前 HEAD 的可回退 first-parent 祖先")


def _rollback_steps() -> List[Dict[str, str]]:
    return [
        {"key": "rollback", "label": "回退代码版本", "status": "active"},
        {"key": "restart", "label": "重启服务", "status": "pending"},
    ]


def _set_rollback_step(key: str, status: str) -> None:
    with repo_update._state_lock:
        for step in repo_update._state["steps"]:
            if step["key"] == key:
                step["status"] = status


def _set_rollback_error(error: Exception) -> None:
    repo_update._set_state(phase="error", running=False, message="版本回退失败", last_error=str(error))
    with repo_update._state_lock:
        for step in repo_update._state["steps"]:
            if step["status"] == "active":
                step["status"] = "error"


def _remote_evidence(remote: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "operation_id",
        "rollback_from",
        "rollback_target",
        "backup",
        "checkout_sha",
        "deployed_sha",
        "deployed_current",
        "deployment_consistent",
    )
    return {key: remote[key] for key in keys if key in remote}


def _begin_state(
    target: Dict[str, Any], current: Dict[str, Any] | None, operation_id: str
) -> None:
    repo_update._set_state(
        phase="queued_rollback",
        running=True,
        trigger="rollback",
        operation_id=operation_id,
        steps=_rollback_steps(),
        message="版本回退已排队，自动更新已关闭",
        last_error="",
        rollback_target=dict(target),
        current=current,
    )


def _disable_auto_update(session: Session) -> Dict[str, Any]:
    config = repo_update.get_config(session)
    return repo_update.set_config(
        session,
        auto_enabled=False,
        interval_seconds=int(config["interval_seconds"]),
    )


def _start_remote_rollback(target: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
    try:
        result = repo_update._remote_request(
            "POST",
            "/rollback",
            {"target_sha": target["sha"], "operation_id": operation_id},
            timeout=15,
        )
    except repo_update.RepoUpdateError as exc:
        if "409" in str(exc):
            raise RepoRollbackBusy("另一个版本更新或回退操作正在进行") from exc
        if "400" in str(exc):
            raise RepoRollbackError("宿主仓库状态已变化或工作树不干净，回退被拒绝") from exc
        raise
    if result.get("busy"):
        raise RepoRollbackBusy(str(result.get("error") or "另一个版本操作正在进行"))
    if not result.get("started"):
        raise RepoRollbackError(str(result.get("error") or "宿主更新服务未接受回退请求"))
    if str(result.get("operation_id") or "") != operation_id:
        raise RepoRollbackError("宿主更新服务返回了不匹配的回退操作标识")
    return result


def start_rollback(session: Session, target_sha: str) -> Dict[str, Any]:
    """Disable automatic updates, then queue a validated rollback."""
    target, mode = _selected_version(target_sha)
    if not repo_update._op_lock.acquire(blocking=False):
        raise RepoRollbackBusy("另一个版本更新或回退操作正在进行")
    try:
        current = repo_update.collect_version_info().get("current")
        config = _disable_auto_update(session)
        operation_id = secrets.token_hex(8)
        _begin_state(target, current if isinstance(current, dict) else None, operation_id)
        if mode != "remote":
            raise RepoRollbackError("回退执行器在验证后变为不可用")
        remote = _start_remote_rollback(target, operation_id)
        target_sha = str(remote.get("target_sha") or target["sha"])
        from_sha = str(remote.get("from_sha") or ((current or {}).get("sha") or ""))
        return {
            "ok": True,
            "started": True,
            "operation_id": operation_id,
            "from_sha": from_sha,
            "target_sha": target_sha,
            "auto_update_disabled": True,
            "warning": ROLLBACK_WARNING,
            "config": config,
            "state": repo_update.get_state(),
        }
    except Exception as exc:
        _set_rollback_error(exc)
        raise
    finally:
        repo_update._op_lock.release()


def _matching_remote_rollback() -> tuple[Dict[str, Any], Dict[str, Any]] | None:
    try:
        payload = repo_update._remote_request("GET", "/state", timeout=5)
    except Exception:
        return None
    remote = payload.get("state")
    if not isinstance(remote, dict) or remote.get("operation") != "rollback":
        return None
    state = repo_update.get_state()
    remote_operation_id = str(remote.get("operation_id") or "")
    expected_operation_id = str(state.get("operation_id") or "")
    if state.get("trigger") == "rollback" and expected_operation_id != remote_operation_id:
        return None
    if state.get("trigger") != "rollback":
        repo_update._set_state(trigger="rollback", steps=_rollback_steps(), **_remote_evidence(remote))
        state = repo_update.get_state()
    return state, remote


def _sync_remote_phase(state: Dict[str, Any], remote: Dict[str, Any]) -> None:
    phase = str(remote.get("phase") or "")
    logs = list(remote.get("logs") or [])[-120:]
    if phase in _ROLLBACK_PHASES:
        repo_update._set_state(phase=phase, running=True, message=str(remote.get("message") or "正在回退版本…"), logs=logs, **_remote_evidence(remote))
    elif phase in {"queued_restart", "rebuilding", "restarting"}:
        _set_rollback_step("rollback", "done"); _set_rollback_step("restart", "active")
        repo_update._set_state(phase=phase, running=True, message=str(remote.get("message") or "正在重建并重启服务…"), logs=logs, **_remote_evidence(remote))
    elif phase == "done":
        _set_rollback_step("rollback", "done"); _set_rollback_step("restart", "done")
        target = state.get("rollback_target")
        repo_update._set_state(phase="done", running=False, message="版本回退完成，自动更新已关闭", last_error="", current=dict(target) if isinstance(target, dict) else state.get("current"), logs=logs, **_remote_evidence(remote))
    elif phase == "error":
        _set_rollback_error(RepoRollbackError(str(remote.get("last_error") or "宿主更新服务回退失败")))
        repo_update._set_state(logs=logs, **_remote_evidence(remote))


def sync_remote_rollback_state() -> None:
    """Mirror host rollback phases while the gateway remains online."""
    if not repo_update.settings.repo_updater_url:
        return
    matched = _matching_remote_rollback()
    if matched:
        _sync_remote_phase(*matched)
