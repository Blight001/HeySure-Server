"""Admin user-management routes."""

import logging
import os
import shutil
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import SQLModel, Session, select

from ai_runtime.inference.ai_service import ensure_default_members_for_user
from api.auth import get_password_hash
from api.core.config import user_workspace_dir
from api.database import get_session
from api.models import User
from gateway.routers.admin import (
    ROLE_LABELS,
    VALID_ROLES,
    _record_audit,
    require_admin_user,
)
from gateway.routers.auth import ensure_user_workspace


logger = logging.getLogger(__name__)
router = APIRouter()
PREFIX = "/api/admin"


class RoleUpdate(BaseModel):
    role: str


class PasswordReset(BaseModel):
    new_password: str


class UserCreatePayload(BaseModel):
    name: str
    account: str
    password: str
    role: str = "member"
    avatar: Optional[str] = None


def serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "account": user.account,
        "avatar": user.avatar,
        "email": user.email,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "created_at": user.created_at,
    }


@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    users = session.exec(select(User).order_by(User.id)).all()
    return {"users": [serialize_user(user) for user in users]}


@router.post("/users")
def create_user(
    payload: UserCreatePayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    name = (payload.name or "").strip()
    account = (payload.account or "").strip()
    password = (payload.password or "").strip()
    role = (payload.role or "member").strip().lower()
    if not name or not account:
        raise HTTPException(status_code=400, detail="昵称和账号不能为空")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="无效的角色")
    if role == "owner" and actor.role != "owner":
        raise HTTPException(status_code=403, detail="只有房主能创建房主")
    if session.exec(select(User).where(User.account == account)).first():
        raise HTTPException(status_code=400, detail="账号已存在")

    new_user = User(
        name=name,
        account=account,
        hashed_password=get_password_hash(password),
        avatar=payload.avatar,
        role=role,
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)
    _bootstrap_user(session, new_user)
    _record_audit(
        session,
        actor,
        "create_user",
        target_type="user",
        target_id=new_user.id,
        target_label=new_user.account,
        detail=f"创建用户 {name}（{account}），权限「{ROLE_LABELS.get(role, role)}」",
    )
    return {"ok": True, "user": serialize_user(new_user)}


def _bootstrap_user(session: Session, user: User) -> None:
    try:
        ensure_user_workspace(user.id)
        ensure_default_members_for_user(session, user.id)
    except Exception:
        logger.exception("post-create bootstrap failed for user %s", user.id)


@router.patch("/users/{user_id}/role")
def set_user_role(
    user_id: int,
    payload: RoleUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    new_role = (payload.role or "").strip().lower()
    if new_role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="无效的角色")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if actor.role != "owner" and (new_role == "owner" or target.role == "owner"):
        raise HTTPException(status_code=403, detail="只有房主能管理房主权限")
    if target.role == "owner" and new_role != "owner":
        _require_another_owner(session, target.id)

    old_role = target.role
    target.role = new_role
    session.add(target)
    session.commit()
    session.refresh(target)
    _record_audit(
        session,
        actor,
        "set_role",
        target_type="user",
        target_id=target.id,
        target_label=target.account,
        detail=f"权限 {ROLE_LABELS.get(old_role, old_role)} → {ROLE_LABELS.get(new_role, new_role)}",
    )
    return {"ok": True, "user": serialize_user(target)}


def _require_another_owner(session: Session, user_id: int) -> None:
    other_owner = session.exec(
        select(User).where(User.role == "owner", User.id != user_id)
    ).first()
    if not other_owner:
        raise HTTPException(status_code=400, detail="至少需要保留一名房主")


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    payload: PasswordReset,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    new_password = (payload.new_password or "").strip()
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if actor.role != "owner" and target.role == "owner":
        raise HTTPException(status_code=403, detail="只有房主能重置房主的密码")

    from api.services.access.session_security import revoke_user_sessions
    from api.socket_events import disconnect_user_sockets

    revoke_user_sessions(session, target)
    target.hashed_password = get_password_hash(new_password)
    session.add(target)
    session.commit()
    disconnect_user_sockets(target.id)
    _record_audit(
        session,
        actor,
        "reset_password",
        target_type="user",
        target_id=target.id,
        target_label=target.account,
        detail=f"重置了 {target.name}（{target.account}）的密码",
    )
    return {"ok": True, "user_id": user_id}


def _delete_user_owned_rows(session: Session, user_id: int) -> None:
    for table in reversed(SQLModel.metadata.sorted_tables):
        if table.name != "user" and "user_id" in table.c:
            session.execute(
                text(f'DELETE FROM "{table.name}" WHERE user_id = :uid'),
                {"uid": user_id},
            )


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if actor.role != "owner" and target.role == "owner":
        raise HTTPException(status_code=403, detail="只有房主能删除房主")
    if target.role == "owner":
        _require_another_owner(session, target.id)

    target_account = target.account
    target_name = target.name
    _delete_user_owned_rows(session, user_id)
    session.delete(target)
    session.commit()
    _remove_user_workspace(user_id)
    logger.warning(
        "admin %s deleted user #%s (%s)", actor.account, user_id, target_account
    )
    _record_audit(
        session,
        actor,
        "delete_user",
        target_type="user",
        target_id=user_id,
        target_label=target_account,
        detail=f"删除用户 {target_name}（{target_account}）及其所有数据",
    )
    return {"ok": True, "user_id": user_id}


def _remove_user_workspace(user_id: int) -> None:
    try:
        workspace = user_workspace_dir(user_id)
        if os.path.isdir(workspace):
            shutil.rmtree(workspace, ignore_errors=True)
    except Exception:
        logger.exception("failed to remove workspace dir for user %s", user_id)
