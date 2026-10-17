"""Host-updater state synchronization for the Gateway process."""

from __future__ import annotations

from typing import Any, Dict, List

from api.services import repo_update


# The Gateway may restart while the host updater is publishing.  Its in-memory
# state then comes back as ``checking``/``idle`` even though the host operation
# has already reached a terminal phase.  Treat all active local phases as
# eligible for terminal-state adoption so the UI cannot remain stuck forever.
_ACTIVE_PHASES = {
    "checking",
    "pulling",
    "backing_up",
    "queued_restart",
    "rebuilding",
    "restarting",
    "rolling_back",
}
_PHASE_MESSAGES = {
    "checking": "宿主更新服务正在检测版本…",
    "backing_up": "宿主更新服务正在备份数据库…",
    "pulling": "宿主更新服务正在拉取代码…",
    "queued_restart": "代码已更新，等待宿主更新服务开始重建…",
    "rebuilding": "正在构建 Docker 镜像…",
    "restarting": "正在重建并启动 Docker 容器…",
    "done": "服务已更新并启动",
    "error": "宿主更新服务重建失败",
}


def _update_steps(phase: str) -> List[Dict[str, str]]:
    steps = repo_update._fresh_steps()
    if phase == "checking":
        steps[0]["status"] = "active"
    elif phase in {"backing_up", "pulling"}:
        steps[0]["status"] = "done"
        steps[1]["status"] = "active"
    else:
        steps[0]["status"] = "done"
        steps[1]["status"] = "done"
        steps[2]["status"] = "active"
    return steps


def _adopt_active(remote_state: Dict[str, Any], phase: str) -> None:
    repo_update._set_state(
        phase=phase,
        running=True,
        trigger=str(remote_state.get("operation") or "remote"),
        operation_id=str(remote_state.get("operation_id") or ""),
        steps=_update_steps(phase),
        message=(
            _PHASE_MESSAGES.get(phase)
            or str(remote_state.get("message") or "宿主版本操作正在进行…")
        ),
        last_error="",
        logs=list(remote_state.get("logs") or [])[-120:],
        release_manifest=remote_state.get("release_manifest"),
    )


def _apply_terminal(remote_state: Dict[str, Any], phase: str) -> None:
    if phase == "done":
        deployed = remote_state.get("deployed_current") or {}
        to_sha = str(deployed.get("sha") or remote_state.get("deployed_sha") or "")
        from_sha = str(remote_state.get("update_from") or "")
        if to_sha:
            repo_update._record_last_update(from_sha=from_sha, to_sha=to_sha)
        repo_update._set_state(
            phase="done",
            running=False,
            message=_PHASE_MESSAGES["done"],
            last_error="",
            logs=list(remote_state.get("logs") or [])[-120:],
            release_manifest=remote_state.get("release_manifest"),
        )
        repo_update._set_step(repo_update._STEP_PULL, "done")
        repo_update._set_step(repo_update._STEP_RESTART, "done")
    elif phase == "error":
        repo_update._set_state(
            phase="error",
            running=False,
            message=_PHASE_MESSAGES["error"],
            last_error=str(remote_state.get("last_error") or "宿主更新服务重建失败"),
            logs=list(remote_state.get("logs") or [])[-120:],
        )
        repo_update._set_step(repo_update._STEP_RESTART, "error")


def sync_remote_update_state() -> None:
    if not repo_update.settings.repo_updater_url:
        return
    try:
        payload = repo_update._remote_request("GET", "/state", timeout=5)
    except Exception as exc:
        repo_update.logger.debug("repo-update: remote state unavailable: %s", exc)
        return
    remote_state = payload.get("state")
    if not isinstance(remote_state, dict) or remote_state.get("operation") == "rollback":
        return
    phase = str(remote_state.get("phase") or "")
    if not phase:
        return
    if bool(remote_state.get("running")):
        _adopt_active(remote_state, phase)
        return
    if str(repo_update.get_state().get("phase") or "") in _ACTIVE_PHASES:
        _apply_terminal(remote_state, phase)
