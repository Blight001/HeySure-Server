"""Admin HTTP routes for Runtime monitoring and ChatRun control."""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from api.database import get_session
from api.models import ChatRun, User
from gateway.routers.admin import _record_audit, require_admin_user
from gateway.routers.admin_services import (
    ServiceRequestError,
    fetch_service_logs,
    list_service_statuses,
    restart_remote_service,
    service_target,
)


logger = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/admin"


def _ensure_repo_operation_idle() -> None:
    """Keep manual service mutations out of an update or rollback window."""
    from api.services import repo_update, repo_versions

    repo_versions.sync_remote_rollback_state()
    repo_update.sync_remote_update_state()
    state = repo_update.get_state()
    if state.get("running"):
        phase = str(state.get("phase") or "unknown")
        raise HTTPException(
            status_code=409,
            detail=f"版本更新或回退正在进行（{phase}），暂不能重启或重建服务",
        )


@router.get("/services")
def list_services(_admin: User = Depends(require_admin_user)) -> dict:
    return {"services": list_service_statuses(), "checked_at": time.time()}


@router.get("/services/{key}/logs")
def service_logs(
    key: str,
    limit: int = 200,
    level: Optional[str] = None,
    _admin: User = Depends(require_admin_user),
) -> dict:
    limit = max(1, min(600, int(limit or 200)))
    target = service_target(key)
    if target is None:
        raise HTTPException(status_code=404, detail="未知的子服务")
    try:
        return fetch_service_logs(target, limit=limit, level=level)
    except ServiceRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tasks")
def list_tasks(
    limit: int = 50,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    limit = max(1, min(200, int(limit or 50)))
    stmt = select(ChatRun).order_by(ChatRun.updated_at.desc()).limit(limit)
    if status:
        stmt = (
            select(ChatRun)
            .where(ChatRun.status == status)
            .order_by(ChatRun.updated_at.desc())
            .limit(limit)
        )
    runs = session.exec(stmt).all()
    user_ids = {run.user_id for run in runs}
    users = {}
    if user_ids:
        users = {
            user.id: user
            for user in session.exec(select(User).where(User.id.in_(user_ids))).all()
        }
    return {
        "tasks": [
            _serialize_task(run, users.get(run.user_id))
            for run in runs
        ]
    }


def _serialize_task(run: ChatRun, owner: Optional[User]) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "stop_requested": run.stop_requested,
        "user_id": run.user_id,
        "user_name": owner.name if owner else None,
        "user_account": owner.account if owner else None,
        "ai_config_id": run.ai_config_id,
        "ai_kind": run.ai_kind,
        "session_id": run.session_id,
        "session_name": run.session_name,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "heartbeat_at": run.heartbeat_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


@router.post("/tasks/{run_id}/stop")
def stop_task(
    run_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    run = session.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
    if not run:
        raise HTTPException(status_code=404, detail="子任务不存在")
    now = time.time()
    run.stop_requested = True
    if run.status in ("queued", "running"):
        run.status = "stopped"
        run.finished_at = run.finished_at or now
    run.updated_at = now
    session.add(run)
    session.commit()
    _record_audit(
        session,
        actor,
        "stop_task",
        target_type="task",
        target_id=run_id,
        target_label=run.session_name or run_id,
        detail=f"停止子任务 {run_id}",
    )
    return {"ok": True, "run_id": run_id, "status": run.status}


@router.post("/services/rebuild-all")
def rebuild_all_services(
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    from api.services import repo_rebuild

    _ensure_repo_operation_idle()
    try:
        payload = repo_rebuild.rebuild_all_containers()
    except repo_rebuild.RepoUpdateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _record_audit(
        session,
        admin,
        "rebuild_all_services",
        target_type="service",
        target_id="all",
        target_label="全部容器",
        detail="重构全部 Docker 容器",
    )
    logger.warning("admin %s triggered rebuild of all containers", admin.account)
    return {"ok": True, "started": True, **payload}


@router.post("/services/{key}/restart")
def restart_service(
    key: str,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    _ensure_repo_operation_idle()
    target = service_target(key)
    if target is None:
        raise HTTPException(status_code=404, detail="未知的子服务")
    if not target.restartable:
        raise HTTPException(status_code=400, detail=f"{target.name} 仅支持状态查看，不能从应用容器内重启")
    if key == "gateway":
        return _restart_gateway(session, admin, target)
    if not target.base_url:
        raise HTTPException(
            status_code=400,
            detail=f"{target.name} 未配置独立服务地址（单体模式无法重启）",
        )
    try:
        payload = restart_remote_service(target)
    except ServiceRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.warning(
        "admin %s triggered restart of %s (%s)",
        admin.account,
        key,
        target.base_url,
    )
    _record_audit(
        session,
        admin,
        "restart_service",
        target_type="service",
        target_id=key,
        target_label=target.name,
        detail=f"重启服务 {target.name}（{target.base_url}）",
    )
    return {"ok": True, "key": key, "name": target.name, **payload}


@router.post("/services/restart-all")
def restart_all_services(
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    """Restart the four application runtimes, scheduling Gateway last."""
    _ensure_repo_operation_idle()
    restarted = []
    errors = {}
    for key in ("mcp", "connector", "ai"):
        target = service_target(key)
        if target is None or not target.base_url:
            continue
        try:
            restart_remote_service(target)
            restarted.append(key)
        except ServiceRequestError as exc:
            errors[key] = str(exc)
    if errors:
        return {"ok": False, "restarting": restarted, "errors": errors, "gateway_scheduled": False}

    from api.runtime.process_control import request_restart

    request_restart(delay=2.0)
    restarted.append("gateway")
    _record_audit(
        session,
        admin,
        "restart_all_services",
        target_type="service",
        target_id="all-runtimes",
        target_label="全部应用 Runtime",
        detail="依次重启 MCP、Connector、AI，并最后重启 Gateway",
    )
    logger.warning("admin %s triggered restart of all application runtimes", admin.account)
    return {"ok": True, "restarting": restarted, "errors": {}, "gateway_scheduled": True}


def _restart_gateway(session: Session, admin: User, target) -> dict:
    from api.runtime.process_control import request_restart

    logger.warning("admin %s triggered gateway restart", admin.account)
    _record_audit(
        session,
        admin,
        "restart_service",
        target_type="service",
        target_id=target.key,
        target_label=target.name,
        detail=f"重启服务 {target.name}（网关自身）",
    )
    command = request_restart(delay=1.0)
    return {
        "ok": True,
        "key": target.key,
        "name": target.name,
        "restarting": True,
        "command": command,
    }
