"""版本 / 仓库自动更新路由：供管理员控制台「版本更新」栏目使用。

- ``GET  /api/admin/repo-update/status`` —— 配置 + 当前进度 + 版本信息
- ``PUT  /api/admin/repo-update/config`` —— 修改自动检测开关与间隔
- ``POST /api/admin/repo-update/check``  —— 立即检测；``apply`` 为真时检测到
  新版本则自动拉取并重启

全部接口仅限房主 / 管理员调用。实际的检测/拉取/重启逻辑都在
``api.services.repo_update``，本文件只做鉴权、参数校验与审计。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from api.database import get_session
from api.models import User
from api.services import repo_update as repo_svc
from .admin import _record_audit, require_admin_user

router = APIRouter()
PREFIX = "/api/admin/repo-update"


def _status_payload(session: Session) -> dict:
    repo_svc.refresh_webhook_state()
    return {
        "config": repo_svc.get_config(session),
        "state": repo_svc.get_state(),
        "version": repo_svc.collect_version_info(),
        "last_update": repo_svc.get_last_update(session),
        "git_available": repo_svc.git_available(),
        "updater_available": repo_svc.updater_available(),
        "update_mode": repo_svc.update_mode(),
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
    cfg = repo_svc.set_config(
        session,
        auto_enabled=payload.auto_enabled,
        interval_seconds=payload.interval_seconds,
    )
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
    apply = payload.apply if payload is not None else True
    _record_audit(
        session, admin, "repo_update_check",
        target_type="repo_update", target_id="check", target_label="版本自动更新",
        detail=f"手动检测（{'检测到即更新' if apply else '仅检测'}）",
    )
    # 在后台线程跑（git/重启是阻塞操作），前端通过 status 轮询看进度。
    repo_svc.trigger_async(trigger="manual", auto_apply=apply)
    return {"ok": True, "started": True, "state": repo_svc.get_state()}
