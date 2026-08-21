"""版本 / 仓库自动更新路由：供管理员控制台「版本更新」栏目使用。

- ``GET  /api/admin/repo-update/status`` —— 配置 + 当前进度 + 版本信息
- ``PUT  /api/admin/repo-update/config`` —— 修改自动检测开关与间隔
- ``GET  /api/admin/repo-update/versions`` —— 可安全回退的版本历史
- ``POST /api/admin/repo-update/rollback`` —— 回退所选版本并关闭自动更新
- ``POST /api/admin/repo-update/check`` —— 检测并按需更新

全部接口仅限房主 / 管理员调用。实际的检测/拉取/重启逻辑都在
``api.services.repo_update``，本文件只做鉴权、参数校验与审计。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from api.database import get_session
from api.models import User
from api.services import repo_update as repo_svc
from api.services import repo_versions
from .admin import _record_audit, require_admin_user

router = APIRouter()
PREFIX = "/api/admin/repo-update"


def _rollback_active() -> bool:
    repo_versions.sync_remote_rollback_state()
    state = repo_svc.get_state()
    return state.get("trigger") == "rollback" and bool(state.get("running"))


def _status_payload(session: Session) -> dict:
    repo_versions.sync_remote_rollback_state()
    if repo_svc.get_state().get("trigger") != "rollback":
        repo_svc.sync_remote_update_state()
    return {
        "config": repo_svc.get_config(session),
        "state": repo_svc.get_state(),
        "version": repo_svc.collect_version_info(),
        "last_update": repo_svc.get_last_update(session),
        "git_available": repo_svc.git_available(),
        "updater_available": repo_svc.updater_available(),
        "update_mode": repo_svc.update_mode(),
        "rollback": {
            "warning": repo_versions.ROLLBACK_WARNING,
            "max_versions": repo_versions.MAX_VERSION_LIMIT,
        },
        "limits": {
            "min_interval": repo_svc.MIN_INTERVAL_SECONDS,
            "max_interval": repo_svc.MAX_INTERVAL_SECONDS,
        },
    }


@router.get("/status")
def repo_update_status(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    return _status_payload(session)


class ConfigUpdate(BaseModel):
    auto_enabled: bool
    interval_seconds: int


@router.put("/config")
def update_config(
    payload: ConfigUpdate,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    owns_lock = False
    if payload.auto_enabled:
        owns_lock = repo_svc._op_lock.acquire(blocking=False)
        if not owns_lock:
            raise HTTPException(status_code=409, detail="版本操作进行中，不能开启自动更新")
    try:
        if payload.auto_enabled and _rollback_active():
            raise HTTPException(status_code=409, detail="版本回退进行中，不能开启自动更新")
        cfg = repo_svc.set_config(
            session,
            auto_enabled=payload.auto_enabled,
            interval_seconds=payload.interval_seconds,
        )
    finally:
        if owns_lock:
            repo_svc._op_lock.release()
    _record_audit(
        session, admin, "repo_update_config",
        target_type="repo_update", target_id="config", target_label="版本自动更新",
        detail=f"自动检测={'开' if cfg['auto_enabled'] else '关'}，间隔={cfg['interval_seconds']}s",
    )
    return _status_payload(session)


class CheckRequest(BaseModel):
    # 默认「检测到即更新」，与自动检测一致；置 false 则仅检测不拉取。
    apply: bool = True


@router.post("/check")
def check_now(
    payload: Optional[CheckRequest] = None,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    if repo_svc._op_lock.locked() or _rollback_active():
        raise HTTPException(status_code=409, detail="版本回退进行中，不能启动版本检测或更新")
    apply = payload.apply if payload is not None else True
    _record_audit(
        session, admin, "repo_update_check",
        target_type="repo_update", target_id="check", target_label="版本自动更新",
        detail=f"手动检测（{'检测到即更新' if apply else '仅检测'}）",
    )
    # 在后台线程跑（git/重启是阻塞操作），前端通过 status 轮询看进度。
    repo_svc.trigger_async(trigger="manual", auto_apply=apply)
    return {"ok": True, "started": True, "state": repo_svc.get_state()}


@router.get("/versions")
def version_history(
    limit: int = Query(repo_versions.DEFAULT_VERSION_LIMIT, ge=1, le=repo_versions.MAX_VERSION_LIMIT),
    _admin: User = Depends(require_admin_user),
) -> dict:
    try:
        return repo_versions.list_versions(limit)
    except repo_versions.RepoRollbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except repo_svc.RepoUpdateError as exc:
        raise HTTPException(status_code=503, detail="无法读取宿主版本历史") from exc


class RollbackRequest(BaseModel):
    target_sha: str


@router.post("/rollback")
def rollback_version(
    payload: RollbackRequest,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    try:
        result = repo_versions.start_rollback(session, payload.target_sha)
    except repo_versions.RepoRollbackBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except repo_versions.RepoRollbackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except repo_svc.RepoUpdateError as exc:
        raise HTTPException(status_code=503, detail="宿主更新服务未接受回退请求") from exc
    _record_audit(
        session,
        admin,
        "repo_update_rollback",
        target_type="repo_update",
        target_id=result["target_sha"],
        target_label="版本回退",
        detail=(
            f"已接受回退 {result.get('from_sha', '')[:12]} -> "
            f"{result['target_sha'][:12]}，自动更新已关闭，操作={result['operation_id']}"
        ),
    )
    return result
