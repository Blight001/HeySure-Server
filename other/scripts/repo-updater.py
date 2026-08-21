#!/usr/bin/env python3
"""Host-side updater for container deployments.

The API gateway runs inside Docker and often does not have the host Git
workspace mounted. This tiny HTTP service runs on the host, receives requests
from the gateway, and performs the real deployment update in the workspace:

    git fetch -> git pull --ff-only -> git submodule update -> docker compose up

Bind it to 0.0.0.0 and expose it to containers through host.docker.internal.
"""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from repo_updater_versions import (
        DEFAULT_VERSION_LIMIT,
        MAX_VERSION_LIMIT,
        VersionSelectionError,
        rollback_candidate,
        safe_compose_definition,
        version_history,
    )
except ModuleNotFoundError:
    from other.scripts.repo_updater_versions import (
        DEFAULT_VERSION_LIMIT,
        MAX_VERSION_LIMIT,
        VersionSelectionError,
        rollback_candidate,
        safe_compose_definition,
        version_history,
    )
try:
    from repo_updater_release import (
        DEFAULT_DATA_ROOT,
        ReleaseSafetyError,
        deploy_release,
        prepare_release,
        validate_rendered_compose,
    )
except ModuleNotFoundError:
    from other.scripts.repo_updater_release import (
        DEFAULT_DATA_ROOT,
        ReleaseSafetyError,
        deploy_release,
        prepare_release,
        validate_rendered_compose,
    )
try:
    from repo_updater_metadata import (
        commit_info,
        compare_remote,
        deployment_snapshot,
        live_version,
        read_deployed_version,
        write_deployed_version,
    )
except ModuleNotFoundError:
    from other.scripts.repo_updater_metadata import (
        commit_info,
        compare_remote,
        deployment_snapshot,
        live_version,
        read_deployed_version,
        write_deployed_version,
    )
try:
    from repo_updater_commands import run_command, run_streaming
except ModuleNotFoundError:
    from other.scripts.repo_updater_commands import run_command, run_streaming
try:
    from repo_updater_checkout import (
        ensure_clean_worktree,
        fast_forward_to,
        restore_checkout,
        sync_deployment_submodules,
        verify_deployment_checkout,
    )
except ModuleNotFoundError:
    from other.scripts.repo_updater_checkout import (
        ensure_clean_worktree,
        fast_forward_to,
        restore_checkout,
        sync_deployment_submodules,
        verify_deployment_checkout,
    )
try:
    from repo_updater_http import make_handler
except ModuleNotFoundError:
    from other.scripts.repo_updater_http import make_handler


REQUIRED_ROOT = Path("/www/server/panel/data/compose/heysureai2")
ROOT = Path(os.environ.get("HEYSURE_REPO_ROOT") or REQUIRED_ROOT).resolve()
ALLOW_NONSTANDARD_ROOT = os.name == "nt" or os.environ.get(
    "HEYSURE_REPO_UPDATER_ALLOW_NONSTANDARD_ROOT"
) == "1"
HOST = os.environ.get("HEYSURE_REPO_UPDATER_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEYSURE_REPO_UPDATER_PORT", "58151"))
COMPOSE_CMD = shlex.split(os.environ.get("HEYSURE_REPO_UPDATER_COMPOSE_CMD", "docker compose"))
DATA_ROOT = (ROOT / "deploy/server/data" if ROOT != REQUIRED_ROOT else DEFAULT_DATA_ROOT).resolve()
VERSION_FILE = DATA_ROOT / "app" / "deployed-version.json"


def _read_dotenv_key(name: str) -> str:
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{name}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value.strip()
    return ""


TOKEN = (
    os.environ.get("HEYSURE_REPO_UPDATER_TOKEN")
    or os.environ.get("HEYSURE_INTERNAL_TOKEN")
    or _read_dotenv_key("HEYSURE_REPO_UPDATER_TOKEN")
    or _read_dotenv_key("HEYSURE_INTERNAL_TOKEN")
    or ""
).strip()

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "message": "",
    "last_error": "",
    "logs": [],
    "updated_at": time.time(),
}


class UpdateError(RuntimeError):
    pass


def _set_state(**fields: Any) -> None:
    _state.update(fields)
    _state["updated_at"] = time.time()


def _append_log(line: str) -> None:
    text = line.strip()
    if not text:
        return
    logs = list(_state.get("logs") or [])
    logs.append(text)
    _state["logs"] = logs[-120:]
    _state["updated_at"] = time.time()
    print(text, flush=True)


def _run(cmd: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    return run_command(ROOT, cmd, timeout, UpdateError)


def _run_streaming(
    cmd: list[str],
    timeout: float = 1800.0,
    environment: dict[str, str] | None = None,
) -> None:
    run_streaming(ROOT, cmd, timeout, environment, _append_log, UpdateError)


def _git(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], timeout=timeout)


def _git_output(args: list[str], timeout: float = 120.0) -> str:
    return _git(args, timeout=timeout).stdout.strip()


def _server_git_output(args: list[str], timeout: float = 120.0) -> str:
    command = ["git", "-C", str(ROOT / "deploy" / "server"), *args]
    return _run(command, timeout=timeout).stdout.strip()


def _read_version_file() -> dict[str, Any] | None:
    return read_deployed_version(VERSION_FILE)


def _branch() -> str:
    return _git_output(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)


def _commit_info(ref: str = "HEAD") -> dict[str, Any] | None:
    return commit_info(_git_output, ref)


def _version_history(limit: int = DEFAULT_VERSION_LIMIT) -> dict[str, Any]:
    return version_history(_git_output, _server_git_output, limit)


def _rollback_candidate(target_sha: str) -> tuple[str, dict[str, Any]]:
    result = rollback_candidate(_git_output, _server_git_output, target_sha)
    ensure_clean_worktree(_git, UpdateError)
    return result


def _version() -> dict[str, Any]:
    return live_version(_git_output, VERSION_FILE)


def _deployment_blocked() -> bool:
    if _state.get("deployment_consistent") is False:
        return True
    cached = _read_version_file()
    if not cached:
        return False
    try:
        checkout_sha = _git_output(["rev-parse", "HEAD"], timeout=15)
    except Exception:
        return True
    return ((cached.get("current") or {}).get("sha") or "") != checkout_sha


def _safe_version_history(limit: int) -> dict[str, Any]:
    payload = _version_history(limit)
    blocked = _deployment_blocked()
    if blocked or ROOT != REQUIRED_ROOT:
        for version in payload["versions"]:
            version["rollback_eligible"] = False
            version["disabled_reason"] = (
                "当前部署未完成或不一致，请先重建当前版本"
                if blocked else "仅受管生产宿主支持安全回退"
            )
    payload["deployment_blocked"] = blocked
    payload["rollback_supported"] = ROOT == REQUIRED_ROOT
    return payload


def _compare() -> dict[str, Any]:
    return compare_remote(_git_output)


def _write_version_file(payload: dict[str, Any]) -> None:
    write_deployed_version(VERSION_FILE, payload)


def _set_release_phase(phase: str, message: str) -> None:
    _set_state(phase=phase, message=message)


def _validate_checked_out_compose() -> None:
    validate_rendered_compose(ROOT, DATA_ROOT, COMPOSE_CMD, _run)


def _prepare_checked_out_release(target_sha: str, deployed_sha: str) -> None:
    _set_state(phase="backing_up", message="creating PostgreSQL backup")
    backup, size = prepare_release(ROOT, DATA_ROOT, COMPOSE_CMD, _run, target_sha)
    _set_state(backup={"path": str(backup), "size": size})
    _append_log(f"PostgreSQL 备份已验证：{backup} ({size} bytes)")
    if _read_version_file() is None:
        version = _deployment_snapshot()
        if ((version.get("current") or {}).get("sha") or "") != deployed_sha:
            raise ReleaseSafetyError("checkout changed before deployed-version marker could be seeded")
        _run(["curl", "-fsS", "http://127.0.0.1:3000/"], timeout=30)
        _run(["curl", "-fsS", "http://127.0.0.1:58150/"], timeout=30)
        _write_version_file(version)
        _append_log("已从通过 HTTP 验收的当前版本初始化部署版本标记")


def _deployment_snapshot() -> dict[str, Any]:
    return deployment_snapshot(_git_output)


def _verify_expected_checkout(expected_sha: str) -> None:
    if _git_output(["rev-parse", "HEAD"], timeout=15) != expected_sha:
        raise ReleaseSafetyError("repository HEAD changed during release")
    verify_deployment_checkout(_git, _git_output, UpdateError)


def _publish_checked_out_version(expected_sha: str) -> None:
    _verify_expected_checkout(expected_sha)
    deploy_release(
        ROOT,
        DATA_ROOT,
        COMPOSE_CMD,
        _run,
        _run_streaming,
        _append_log,
        _set_release_phase,
    )
    _verify_expected_checkout(expected_sha)
    version = _deployment_snapshot()
    _write_version_file(version)
    _append_log("Runtime 与 Web 已通过发布验收")
    _set_state(
        running=False,
        phase="done",
        message="services updated",
        last_error="",
        deployed_current=version.get("current"),
        checkout_sha=((version.get("current") or {}).get("sha")),
        deployed_sha=((version.get("current") or {}).get("sha")),
        deployment_consistent=True,
    )


def _mark_release_failed(message: str, exc: Exception) -> None:
    cached = _read_version_file() or {}
    try:
        checkout = _commit_info("HEAD")
    except Exception:
        checkout = None
    checkout_sha = (checkout or {}).get("sha")
    deployed_sha = (cached.get("current") or {}).get("sha")
    expected_sha = _state.get("rollback_from") or _state.get("update_from")
    checkout_consistent = bool(expected_sha and checkout_sha == expected_sha)
    if checkout_consistent:
        try:
            verify_deployment_checkout(_git, _git_output, UpdateError)
        except Exception:
            checkout_consistent = False
    _set_state(
        running=False,
        phase="error",
        message=message,
        last_error="发布未通过安全门禁或 readiness 验收，请查看宿主日志",
        checkout_sha=checkout_sha,
        deployed_sha=deployed_sha,
        deployment_consistent=checkout_consistent,
    )
    _append_log(f"发布失败（{type(exc).__name__}），未写入部署版本标记")
    print(f"host updater release failure: {type(exc).__name__}", flush=True)


def _rebuild_worker(prepared: bool = False) -> None:
    try:
        target_sha = _git_output(["rev-parse", "HEAD"], timeout=15)
        if not prepared:
            _prepare_checked_out_release(target_sha, target_sha)
        _publish_checked_out_version(target_sha)
    except Exception as exc:
        _mark_release_failed("update failed", exc)
    finally:
        _lock.release()


def _queue_rebuild() -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "another update or rebuild is already running", "state": dict(_state)}
    if _state.get("running"):
        _lock.release()
        return {"ok": False, "busy": True, "error": "another update or rebuild is already running", "state": dict(_state)}
    operation_id = secrets.token_hex(8)
    _set_state(
        running=True,
        phase="queued_restart",
        operation="rebuild",
        operation_id=operation_id,
        message="manual compose rebuild queued",
        last_error="",
        logs=[],
    )
    _append_log("管理员已请求重构全部 Docker 容器")
    threading.Thread(target=_rebuild_worker, name="heysure-compose-rebuild", daemon=True).start()
    return {"ok": True, "started": True, "state": dict(_state)}


def _rollback_worker(from_sha: str, target: dict[str, Any]) -> None:
    reset_done = False
    publish_started = False
    try:
        verified_from, verified_target = _rollback_candidate(str(target["sha"]))
        if verified_from != from_sha or verified_target["sha"] != target["sha"]:
            raise VersionSelectionError("版本历史已变化，请重新选择回退版本")
        ensure_clean_worktree(_git, UpdateError)
        target_sha = str(target["sha"])
        _prepare_checked_out_release(target_sha, from_sha)
        verified_from, verified_target = _rollback_candidate(target_sha)
        if verified_from != from_sha or verified_target["sha"] != target_sha:
            raise VersionSelectionError("备份期间版本历史已变化，请重新选择回退版本")
        ensure_clean_worktree(_git, UpdateError)
        _set_state(phase="rolling_back", message="resetting repository to selected version")
        _append_log(f"开始回退代码版本：{from_sha[:8]} -> {target_sha[:8]}")
        _git(["reset", "--hard", target_sha], timeout=120)
        reset_done = True
        if _git_output(["rev-parse", "HEAD"], timeout=15) != target_sha:
            raise UpdateError("repository HEAD does not match rollback target")
        sync_deployment_submodules(_git, _git_output, UpdateError)
        if not safe_compose_definition(_git_output, "HEAD"):
            raise ReleaseSafetyError("target compose definition failed the persistence safety gate")
        _validate_checked_out_compose()
        _set_state(phase="queued_restart", message="rollback complete; compose rebuild queued")
        _append_log("代码版本已回退，已排队重建 Docker 服务")
        publish_started = True
        _publish_checked_out_version(target_sha)
    except Exception as exc:
        if reset_done and not publish_started:
            try:
                restore_checkout(_git, _git_output, UpdateError, from_sha)
                _append_log("发布开始前失败，已恢复原工作区版本")
            except Exception:
                _append_log("原工作区版本自动恢复失败，请人工核对")
        _mark_release_failed("rollback failed", exc)
    finally:
        _lock.release()


def _queue_rollback(target_sha: str, operation_id: str = "") -> tuple[int, dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return 409, {
            "ok": False,
            "busy": True,
            "error": "another update, rollback or rebuild is already running",
            "state": dict(_state),
        }
    if ROOT != REQUIRED_ROOT:
        _lock.release()
        return 409, {"ok": False, "error": "仅受管生产宿主支持安全回退"}
    if _deployment_blocked():
        _lock.release()
        return 409, {"ok": False, "error": "当前部署不一致，请先重建当前版本"}
    try:
        from_sha, target = _rollback_candidate(target_sha)
    except VersionSelectionError as exc:
        _lock.release()
        return 400, {"ok": False, "error": str(exc), "state": dict(_state)}
    except Exception:
        _lock.release()
        return 400, {"ok": False, "error": "版本回退安全校验失败", "state": dict(_state)}
    valid_operation_id = len(operation_id) == 16 and all(
        character in "0123456789abcdef" for character in operation_id.lower()
    )
    accepted_operation_id = operation_id.lower() if valid_operation_id else secrets.token_hex(8)
    _set_state(
        running=True,
        phase="queued_rollback",
        operation="rollback",
        operation_id=accepted_operation_id,
        message="repository rollback queued",
        last_error="",
        logs=[],
        rollback_from=from_sha,
        rollback_target=target,
    )
    threading.Thread(
        target=_rollback_worker,
        args=(from_sha, target),
        name="heysure-repo-rollback",
        daemon=True,
    ).start()
    return 202, {
        "ok": True,
        "started": True,
        "operation_id": accepted_operation_id,
        "from_sha": from_sha,
        "target_sha": str(target["sha"]),
        "state": dict(_state),
    }


def _check_and_update(apply: bool) -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "state": dict(_state)}
    release_lock = True
    reset_done = False
    publish_queued = False
    try:
        if apply and _deployment_blocked():
            return {"ok": False, "blocked": True, "error": "当前部署不一致，请先重建当前版本"}
        _set_state(
            running=True,
            phase="checking",
            operation="update",
            operation_id=secrets.token_hex(8),
            message="checking remote updates",
            last_error="",
            logs=[],
        )
        _append_log("开始检测远程更新...")
        info = _compare()
        if info["ahead"] > 0:
            raise UpdateError("local branch has commits that are not on the deployment upstream")
        if info["behind"] <= 0:
            _set_state(running=False, phase="up_to_date", message="already up to date")
            return {"ok": True, "updated": False, "update_available": False, **info, "state": dict(_state)}
        if not apply:
            _set_state(running=False, phase="update_available", message=f"{info['behind']} commits available")
            return {"ok": True, "updated": False, "update_available": True, **info, "state": dict(_state)}

        from_sha = ((info.get("current") or {}).get("sha") or "")
        target_sha = ((info.get("remote") or {}).get("sha") or "")
        _set_state(update_from=from_sha, update_target=target_sha)
        ensure_clean_worktree(_git, UpdateError)
        _prepare_checked_out_release(target_sha, from_sha)
        ensure_clean_worktree(_git, UpdateError)
        if _git_output(["rev-parse", "HEAD"], timeout=15) != from_sha:
            raise UpdateError("repository HEAD changed while the backup was running")
        _set_state(phase="pulling", message="pulling latest code")
        _append_log("开始拉取最新代码...")
        fast_forward_to(_git, str(info.get("upstream") or ""))
        reset_done = True
        if _git_output(["rev-parse", "HEAD"], timeout=15) != target_sha:
            raise UpdateError("repository HEAD does not match fetched update target")
        sync_deployment_submodules(_git, _git_output, UpdateError)
        if not safe_compose_definition(_git_output, "HEAD"):
            raise ReleaseSafetyError("target compose definition failed the persistence safety gate")
        _validate_checked_out_compose()
        version = _deployment_snapshot()
        to_sha = ((version.get("current") or {}).get("sha") or "")
        _set_state(phase="queued_restart", message="compose rebuild queued")
        _append_log("代码已更新，已排队重建 Docker 服务")
        threading.Thread(
            target=_rebuild_worker,
            args=(True,),
            name="heysure-compose-rebuild",
            daemon=True,
        ).start()
        publish_queued = True
        release_lock = False
        return {
            "ok": True,
            "updated": True,
            "restarting": True,
            "from": from_sha,
            "to": to_sha,
            **info,
            "current": version.get("current"),
            "state": dict(_state),
        }
    except Exception as exc:
        if reset_done and not publish_queued:
            try:
                restore_checkout(_git, _git_output, UpdateError, from_sha)
            except Exception:
                _append_log("更新发布前失败且原工作区自动恢复失败")
        _mark_release_failed("update failed", exc)
        return {"ok": False, "error": "更新未通过安全门禁", "state": dict(_state)}
    finally:
        if release_lock:
            _lock.release()


def main() -> None:
    if len(TOKEN) < 32:
        raise SystemExit("repo updater token is required and must contain at least 32 characters")
    if ROOT != REQUIRED_ROOT and not ALLOW_NONSTANDARD_ROOT:
        raise SystemExit("repo updater root must be /www/server/panel/data/compose/heysureai2")
    try:
        _version()
    except Exception as exc:
        print(f"repo updater could not collect startup version: {exc}", flush=True)
    token_note = "configured" if TOKEN else "disabled"
    print(f"HeySure repo updater listening on http://{HOST}:{PORT} (root={ROOT}, token={token_note})", flush=True)
    handler = make_handler({
        "token": TOKEN,
        "default_version_limit": DEFAULT_VERSION_LIMIT,
        "version": _version,
        "version_history": _safe_version_history,
        "state": _state,
        "check": _check_and_update,
        "rollback": _queue_rollback,
        "rebuild": _queue_rebuild,
    })
    ThreadingHTTPServer((HOST, PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
