"""Admin panel API — service monitoring + user management.

All routes are gated to platform staff (``owner`` / ``admin``). The owner
(房主) is the only tier that can change roles or touch another owner; admins
(管理员) can monitor services, restart sub-tasks, list members and reset
member passwords.

Mounted at ``/api/admin`` (see ``PREFIX``) and auto-discovered by
``gateway.app``.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import (
    and_,
    delete as sa_delete,
    func,
    insert as sa_insert,
    inspect as sa_inspect,
    or_,
    text,
    update as sa_update,
)
from sqlmodel import Session, SQLModel, select

from ai_runtime.inference.ai_service import ensure_default_ai_for_user
from api.auth import get_password_hash, verify_password
from api.core.config import DATA_DIR, user_workspace_dir
from api.core.logging_config import get_recent_logs
from api.core.settings import settings
from api.database import engine, get_session
from api.models import (
    AdminAuditLog,
    AgentDispatchTask,
    AIMessage,
    AITaskJob,
    ChatMessage,
    ChatRun,
    ChatSession,
    EvolutionProject,
    Memory,
    TokenUsageSnapshot,
    User,
)
from api.runtime.internal_http import InternalClient
from gateway.routers.auth import ensure_user_workspace, get_current_user


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/admin"

VALID_ROLES = ("owner", "admin", "member")
ROLE_LABELS = {"owner": "房主", "admin": "管理员", "member": "成员"}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def require_admin_user(authorization: str = Header(None), session: Session = Depends(get_session)) -> User:
    """Resolve the caller and require an owner/admin tier."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    user = get_current_user(authorization, session)
    if user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="需要管理员或房主权限")
    return user


def require_owner_user(authorization: str = Header(None), session: Session = Depends(get_session)) -> User:
    """Resolve the caller and require the owner (房主) tier.

    Used to gate raw database writes: editing rows directly bypasses the
    safeguards baked into the typed endpoints (e.g. an admin must not be able
    to grant themselves ``owner`` by editing the ``user`` table), so mutating
    the database is reserved for owners.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    user = get_current_user(authorization, session)
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="需要房主权限")
    return user


def _record_audit(
    session: Session,
    actor: User,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    target_label: str = "",
    detail: str = "",
) -> None:
    """Persist a privileged action. Best-effort: a logging failure must not
    abort the action the admin actually requested."""
    try:
        session.add(
            AdminAuditLog(
                actor_id=actor.id,
                actor_account=actor.account,
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                target_label=target_label,
                detail=detail,
            )
        )
        session.commit()
    except Exception:
        logger.exception("failed to write admin audit log")
        session.rollback()


# ---------------------------------------------------------------------------
# Service monitoring
# ---------------------------------------------------------------------------


def _service_registry():
    """(key, display name, base_url) for every monitorable sub-service.

    ``gateway`` is this very process, so it has no URL (handled in-process).
    The others are reachable only when their ``*_runtime_url`` env is set
    (i.e. a split/compose deployment); in a monolith they show as ``local``.
    """
    return [
        ("gateway", "API 网关", ""),
        ("mcp", "MCP 运行时", settings.mcp_runtime_url),
        ("connector", "连接器运行时", settings.connector_runtime_url),
        ("ai", "AI 运行时", settings.ai_runtime_url),
    ]


def _probe_service(key: str, name: str, base_url: str) -> dict:
    if key == "gateway":
        return {
            "key": key,
            "name": name,
            "status": "running",
            "detail": {"role": "gateway"},
            "url": "(self)",
        }
    if not base_url:
        return {
            "key": key,
            "name": name,
            "status": "local",
            "detail": {"note": "未配置独立服务地址（单体模式）"},
            "url": "",
        }
    client = InternalClient(base_url, timeout=4.0)
    try:
        payload = client.get("/internal/health")
        status = "running" if payload.get("ok") else "degraded"
        return {"key": key, "name": name, "status": status, "detail": payload, "url": base_url}
    except Exception as exc:  # network error / non-2xx / timeout
        return {
            "key": key,
            "name": name,
            "status": "down",
            "detail": {"error": str(exc)},
            "url": base_url,
        }
    finally:
        client.close()


@router.get("/services")
def list_services(_admin: User = Depends(require_admin_user)) -> dict:
    services = [_probe_service(key, name, url) for key, name, url in _service_registry()]
    return {"services": services, "checked_at": time.time()}


@router.get("/services/{key}/logs")
def service_logs(
    key: str,
    limit: int = 200,
    level: Optional[str] = None,
    _admin: User = Depends(require_admin_user),
) -> dict:
    limit = max(1, min(600, int(limit or 200)))
    registry = {k: (name, url) for k, name, url in _service_registry()}
    if key not in registry:
        raise HTTPException(status_code=404, detail="未知的子服务")
    name, base_url = registry[key]

    if key == "gateway":
        return {"key": key, "name": name, "lines": get_recent_logs(limit=limit, level=level)}
    if not base_url:
        return {"key": key, "name": name, "lines": [], "note": "单体模式：日志与网关合并"}
    client = InternalClient(base_url, timeout=4.0)
    try:
        params = {"limit": limit}
        if level:
            params["level"] = level
        payload = client.get("/internal/logs", params=params)
        return {"key": key, "name": name, "lines": payload.get("lines", [])}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"无法获取 {name} 日志: {exc}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Sub-task monitoring + control (ChatRun)
# ---------------------------------------------------------------------------


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
        stmt = select(ChatRun).where(ChatRun.status == status).order_by(ChatRun.updated_at.desc()).limit(limit)
    runs = session.exec(stmt).all()

    # Batch the owning-account lookup so the list stays one extra query.
    user_ids = {r.user_id for r in runs}
    users = {}
    if user_ids:
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all():
            users[u.id] = u

    tasks = []
    for r in runs:
        owner = users.get(r.user_id)
        tasks.append(
            {
                "run_id": r.run_id,
                "status": r.status,
                "stop_requested": r.stop_requested,
                "user_id": r.user_id,
                "user_name": owner.name if owner else None,
                "user_account": owner.account if owner else None,
                "ai_config_id": r.ai_config_id,
                "ai_kind": r.ai_kind,
                "session_id": r.session_id,
                "session_name": r.session_name,
                "error_message": r.error_message,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "heartbeat_at": r.heartbeat_at,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
        )
    return {"tasks": tasks}


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
        session, actor, "stop_task",
        target_type="task", target_id=run_id, target_label=run.session_name or run_id,
        detail=f"停止子任务 {run_id}",
    )
    return {"ok": True, "run_id": run_id, "status": run.status}


@router.post("/services/{key}/restart")
def restart_service(
    key: str,
    session: Session = Depends(get_session),
    admin: User = Depends(require_admin_user),
) -> dict:
    """Restart a whole sub-service (its process/port), not an individual run.

    - ``gateway`` restarts this very process — the connection serving this
      request drops and the service comes back up on the same port.
    - The remote runtimes are told to re-exec themselves via their own
      ``/internal/restart`` endpoint.
    - In monolith mode (no dedicated URL) there is no separate process to
      bounce, so the call is rejected.
    """
    registry = {k: (name, url) for k, name, url in _service_registry()}
    if key not in registry:
        raise HTTPException(status_code=404, detail="未知的子服务")
    name, base_url = registry[key]

    if key == "gateway":
        from api.runtime.process_control import request_restart

        logger.warning(f"admin {admin.account} triggered gateway restart")
        _record_audit(
            session, admin, "restart_service",
            target_type="service", target_id=key, target_label=name,
            detail=f"重启服务 {name}（网关自身）",
        )
        cmd = request_restart(delay=1.0)
        return {"ok": True, "key": key, "name": name, "restarting": True, "command": cmd}

    if not base_url:
        raise HTTPException(status_code=400, detail=f"{name} 未配置独立服务地址（单体模式无法重启）")

    client = InternalClient(base_url, timeout=5.0)
    try:
        payload = client.post("/internal/restart")
        logger.warning(f"admin {admin.account} triggered restart of {key} ({base_url})")
        _record_audit(
            session, admin, "restart_service",
            target_type="service", target_id=key, target_label=name,
            detail=f"重启服务 {name}（{base_url}）",
        )
        return {"ok": True, "key": key, "name": name, **payload}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"无法重启 {name}: {exc}")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


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


def _serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "account": u.account,
        "avatar": u.avatar,
        "email": u.email,
        "role": u.role,
        "role_label": ROLE_LABELS.get(u.role, u.role),
        "created_at": u.created_at,
    }


@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    users = session.exec(select(User).order_by(User.id)).all()
    return {"users": [_serialize_user(u) for u in users]}


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
    # Only an owner may mint another owner.
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

    # Mirror normal registration so the account is immediately usable.
    try:
        ensure_user_workspace(new_user.id)
        ensure_default_ai_for_user(session, new_user.id)
    except Exception:
        logger.exception(f"post-create bootstrap failed for user {new_user.id}")

    _record_audit(
        session, actor, "create_user",
        target_type="user", target_id=new_user.id, target_label=new_user.account,
        detail=f"创建用户 {name}（{account}），权限「{ROLE_LABELS.get(role, role)}」",
    )
    return {"ok": True, "user": _serialize_user(new_user)}


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

    # Only the owner may grant/revoke the owner tier or touch another owner.
    if actor.role != "owner" and (new_role == "owner" or target.role == "owner"):
        raise HTTPException(status_code=403, detail="只有房主能管理房主权限")

    # Never strand the platform without an owner.
    if target.role == "owner" and new_role != "owner":
        other_owners = session.exec(
            select(User).where(User.role == "owner", User.id != target.id)
        ).first()
        if not other_owners:
            raise HTTPException(status_code=400, detail="至少需要保留一名房主")

    old_role = target.role
    target.role = new_role
    session.add(target)
    session.commit()
    session.refresh(target)
    _record_audit(
        session, actor, "set_role",
        target_type="user", target_id=target.id, target_label=target.account,
        detail=f"权限 {ROLE_LABELS.get(old_role, old_role)} → {ROLE_LABELS.get(new_role, new_role)}",
    )
    return {"ok": True, "user": _serialize_user(target)}


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
    # Admins cannot reset an owner's password; owners can reset anyone.
    if actor.role != "owner" and target.role == "owner":
        raise HTTPException(status_code=403, detail="只有房主能重置房主的密码")

    target.hashed_password = get_password_hash(new_password)
    session.add(target)
    session.commit()
    _record_audit(
        session, actor, "reset_password",
        target_type="user", target_id=target.id, target_label=target.account,
        detail=f"重置了 {target.name}（{target.account}）的密码",
    )
    return {"ok": True, "user_id": user_id}


def _delete_user_owned_rows(session: Session, user_id: int) -> None:
    """Remove every row scoped to ``user_id`` before deleting the account.

    Postgres enforces the ``user_id`` foreign keys, so the account row can't
    go until its dependents do. We walk the SQLModel metadata in reverse
    dependency order and clear any table carrying a ``user_id`` column — this
    stays correct as new user-scoped tables are added without editing here.
    """
    for table in reversed(SQLModel.metadata.sorted_tables):
        if table.name == "user":
            continue
        if "user_id" in table.c:
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
    # Admins may not remove owners; only owners can.
    if actor.role != "owner" and target.role == "owner":
        raise HTTPException(status_code=403, detail="只有房主能删除房主")
    # Never delete the last remaining owner.
    if target.role == "owner":
        other_owner = session.exec(
            select(User).where(User.role == "owner", User.id != target.id)
        ).first()
        if not other_owner:
            raise HTTPException(status_code=400, detail="至少需要保留一名房主")

    target_account = target.account
    target_name = target.name
    _delete_user_owned_rows(session, user_id)
    session.delete(target)
    session.commit()

    # Best-effort: drop the user's on-disk workspace so deletion is complete.
    try:
        ws = user_workspace_dir(user_id)
        if os.path.isdir(ws):
            shutil.rmtree(ws, ignore_errors=True)
    except Exception:
        logger.exception(f"failed to remove workspace dir for user {user_id}")

    logger.warning(f"admin {actor.account} deleted user #{user_id} ({target_account})")
    _record_audit(
        session, actor, "delete_user",
        target_type="user", target_id=user_id, target_label=target_account,
        detail=f"删除用户 {target_name}（{target_account}）及其所有数据",
    )
    return {"ok": True, "user_id": user_id}


# ---------------------------------------------------------------------------
# Data folder file manager
#
# Lets staff browse / view / edit / create / delete files under the server's
# ``data`` directory (``server/data``, mounted at ``/app/data``). Every path is
# resolved with ``realpath`` and checked to live inside ``DATA_ROOT`` so a
# crafted ``..`` or symlink can never escape the sandbox. Writes/deletes are
# audited just like the other privileged actions.
# ---------------------------------------------------------------------------


DATA_ROOT = os.path.realpath(DATA_DIR)
# Cap the size we're willing to load into the in-browser editor. Larger files
# are listed but not opened for editing (they'd freeze the textarea anyway).
MAX_EDIT_BYTES = 1024 * 1024  # 1 MiB


class FileWritePayload(BaseModel):
    path: str
    content: str = ""


class FilePathPayload(BaseModel):
    path: str


class FileRenamePayload(BaseModel):
    path: str
    new_path: str


def _safe_data_path(rel: str) -> str:
    """Resolve ``rel`` against the data root, rejecting any escape attempt.

    Returns the absolute, symlink-resolved path. The result is guaranteed to
    be ``DATA_ROOT`` itself or a descendant of it.
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(DATA_ROOT, rel))
    if full != DATA_ROOT and not full.startswith(DATA_ROOT + os.sep):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    return full


def _rel_to_root(full: str) -> str:
    rel = os.path.relpath(full, DATA_ROOT).replace(os.sep, "/")
    return "" if rel == "." else rel


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}


def _file_kind(name: str) -> str:
    """Coarse classification used by the UI to pick a viewer/icon.

    ``image`` files get an inline preview, ``text`` files open in the editor,
    and everything else (``binary``) is download-only.
    """
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    mime, _ = mimetypes.guess_type(name)
    if mime and (mime.startswith("text/") or mime in ("application/json", "application/xml", "application/javascript")):
        return "text"
    return "text" if not ext else "binary"


def _entry_info(full: str) -> dict:
    st = os.stat(full)
    is_dir = os.path.isdir(full)
    return {
        "name": os.path.basename(full),
        "path": _rel_to_root(full),
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "modified": st.st_mtime,
        "kind": "dir" if is_dir else _file_kind(os.path.basename(full)),
    }


def _is_probably_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


@router.get("/files")
def list_files(path: str = "", _admin: User = Depends(require_admin_user)) -> dict:
    full = _safe_data_path(path)
    # The data dir may not exist yet on a fresh install — present it as empty.
    if not os.path.exists(full):
        if full == DATA_ROOT:
            return {"path": "", "entries": []}
        raise HTTPException(status_code=404, detail="路径不存在")
    if not os.path.isdir(full):
        raise HTTPException(status_code=400, detail="该路径不是文件夹")
    entries = []
    for name in os.listdir(full):
        try:
            entries.append(_entry_info(os.path.join(full, name)))
        except OSError:
            continue  # vanished mid-listing / unreadable — skip it
    # Folders first, then files, each alphabetical (case-insensitive).
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": _rel_to_root(full), "entries": entries}


@router.get("/files/read")
def read_file(path: str, _admin: User = Depends(require_admin_user)) -> dict:
    full = _safe_data_path(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = os.path.getsize(full)
    if size > MAX_EDIT_BYTES:
        return {"path": _rel_to_root(full), "size": size, "binary": False, "too_large": True, "content": ""}
    with open(full, "rb") as f:
        data = f.read()
    if not _is_probably_text(data):
        return {"path": _rel_to_root(full), "size": size, "binary": True, "too_large": False, "content": ""}
    return {
        "path": _rel_to_root(full),
        "size": size,
        "binary": False,
        "too_large": False,
        "content": data.decode("utf-8"),
        "kind": _file_kind(os.path.basename(full)),
    }


@router.get("/files/raw")
def raw_file(path: str, _admin: User = Depends(require_admin_user)):
    """Stream a file's raw bytes — used for image previews and downloads.

    The media type is guessed from the name so browsers render images inline;
    unknown types fall back to ``application/octet-stream``.
    """
    full = _safe_data_path(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type, _ = mimetypes.guess_type(full)
    return FileResponse(full, media_type=media_type or "application/octet-stream")


@router.put("/files")
def write_file(
    payload: FileWritePayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    full = _safe_data_path(payload.path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if os.path.isdir(full):
        raise HTTPException(status_code=400, detail="目标是文件夹，无法写入")
    if len(payload.content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise HTTPException(status_code=400, detail="文件内容过大")
    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(payload.content)
    rel = _rel_to_root(full)
    _record_audit(
        session, actor, "file_write",
        target_type="file", target_id=rel, target_label=rel,
        detail=f"{'修改' if existed else '新建'}文件 data/{rel}",
    )
    return {"ok": True, "path": rel, "created": not existed}


@router.post("/files/mkdir")
def make_dir(
    payload: FilePathPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    full = _safe_data_path(payload.path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件夹路径")
    if os.path.exists(full):
        raise HTTPException(status_code=400, detail="该路径已存在")
    os.makedirs(full, exist_ok=False)
    rel = _rel_to_root(full)
    _record_audit(
        session, actor, "file_mkdir",
        target_type="file", target_id=rel, target_label=rel,
        detail=f"新建文件夹 data/{rel}",
    )
    return {"ok": True, "path": rel}


@router.post("/files/rename")
def rename_path(
    payload: FileRenamePayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    src = _safe_data_path(payload.path)
    dst = _safe_data_path(payload.new_path)
    if src == DATA_ROOT or dst == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="源文件不存在")
    if os.path.exists(dst):
        raise HTTPException(status_code=400, detail="目标已存在")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    src_rel, dst_rel = _rel_to_root(src), _rel_to_root(dst)
    _record_audit(
        session, actor, "file_rename",
        target_type="file", target_id=dst_rel, target_label=dst_rel,
        detail=f"重命名 data/{src_rel} → data/{dst_rel}",
    )
    return {"ok": True, "path": dst_rel}


@router.delete("/files")
def delete_path(
    path: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    full = _safe_data_path(path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="不能删除数据根目录")
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="路径不存在")
    rel = _rel_to_root(full)
    is_dir = os.path.isdir(full)
    if is_dir:
        shutil.rmtree(full, ignore_errors=True)
    else:
        os.remove(full)
    _record_audit(
        session, actor, "file_delete",
        target_type="file", target_id=rel, target_label=rel,
        detail=f"删除{'文件夹' if is_dir else '文件'} data/{rel}",
    )
    return {"ok": True, "path": rel}


class FileBatchPayload(BaseModel):
    paths: list[str]


@router.post("/files/batch-delete")
def batch_delete_paths(
    payload: FileBatchPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    """Delete several files/folders in one call.

    Each path is independently sandboxed; failures are collected per-path so a
    single bad entry doesn't abort the rest of the batch.
    """
    deleted: list[str] = []
    errors: list[dict] = []
    for raw in payload.paths or []:
        try:
            full = _safe_data_path(raw)
            if full == DATA_ROOT:
                raise ValueError("不能删除数据根目录")
            if not os.path.exists(full):
                raise ValueError("路径不存在")
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.remove(full)
            deleted.append(_rel_to_root(full))
        except Exception as exc:
            errors.append({"path": raw, "error": str(exc)})
    if deleted:
        _record_audit(
            session, actor, "file_delete",
            target_type="file", target_id=";".join(deleted[:20]), target_label=f"{len(deleted)} 项",
            detail=f"批量删除 {len(deleted)} 项：" + "、".join(f"data/{p}" for p in deleted[:10])
                   + ("…" if len(deleted) > 10 else ""),
        )
    return {"ok": True, "deleted": deleted, "errors": errors}


# ---------------------------------------------------------------------------
# Database browser
#
# A generic, table-agnostic view over the project's database. Tables and
# columns are discovered from SQLModel's metadata, so every model is browsable
# without bespoke code and new tables show up automatically. Reads are open to
# owner/admin; writes (insert/update/delete) are owner-only because editing
# rows raw bypasses the typed endpoints' safeguards.
# ---------------------------------------------------------------------------


DB_PAGE_MAX = 200


class DbRowInsert(BaseModel):
    values: dict


class DbRowUpdate(BaseModel):
    pk: dict
    values: dict


class DbRowDelete(BaseModel):
    pk: dict


def _db_table(name: str):
    tbl = SQLModel.metadata.tables.get(name)
    if tbl is None:
        raise HTTPException(status_code=404, detail="数据表不存在")
    return tbl


def _col_py_type(col) -> type:
    try:
        return col.type.python_type
    except Exception:
        return str


def _col_info(col) -> dict:
    return {
        "name": col.name,
        "type": str(col.type),
        "py_type": _col_py_type(col).__name__,
        "nullable": bool(col.nullable),
        "primary_key": bool(col.primary_key),
    }


def _json_safe(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


def _coerce_value(col, raw):
    """Coerce a JSON/string value from the client to the column's type."""
    if raw is None:
        return None
    pytype = _col_py_type(col)
    if isinstance(raw, str):
        if pytype is str:
            return raw
        if raw == "":
            return None  # empty string for a non-text column means "unset"
        if pytype is bool:
            return raw.strip().lower() in ("1", "true", "t", "yes", "y", "on")
        if pytype is int:
            try:
                return int(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"字段 {col.name} 需要整数")
        if pytype is float:
            try:
                return float(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"字段 {col.name} 需要数字")
        return raw  # JSON / datetime / etc. — pass the raw text through
    return raw  # already a JSON bool/int/float


def _coerce_values(tbl, values: dict, *, for_insert: bool) -> dict:
    cols = {c.name: c for c in tbl.columns}
    out = {}
    for key, raw in values.items():
        col = cols.get(key)
        if col is None:
            continue  # ignore unknown columns instead of erroring
        # On insert, drop an empty autoincrement PK so the DB assigns one.
        if for_insert and col.primary_key and col.autoincrement and (raw is None or raw == ""):
            continue
        out[key] = _coerce_value(col, raw)
    return out


def _pk_clause(tbl, pk: dict):
    pk_cols = list(tbl.primary_key.columns)
    if not pk_cols:
        raise HTTPException(status_code=400, detail="该表没有主键，无法定位行")
    clauses = []
    for col in pk_cols:
        if col.name not in pk:
            raise HTTPException(status_code=400, detail=f"缺少主键字段 {col.name}")
        clauses.append(col == _coerce_value(col, pk[col.name]))
    return and_(*clauses)


def _pk_label(tbl, values: dict) -> str:
    pk_cols = [c.name for c in tbl.primary_key.columns]
    return ", ".join(f"{c}={values.get(c)}" for c in pk_cols) or "?"


@router.get("/db/tables")
def list_db_tables(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    tables = []
    for tbl in SQLModel.metadata.sorted_tables:
        try:
            count = session.execute(select(func.count()).select_from(tbl)).scalar()
        except Exception:
            count = -1
        tables.append(
            {
                "name": tbl.name,
                "row_count": int(count or 0) if count is not None and count >= 0 else -1,
                "columns": [_col_info(c) for c in tbl.columns],
                "primary_key": [c.name for c in tbl.primary_key.columns],
            }
        )
    tables.sort(key=lambda t: t["name"])
    return {"tables": tables}


@router.get("/db/tables/{name}/rows")
def list_db_rows(
    name: str,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    tbl = _db_table(name)
    limit = max(1, min(DB_PAGE_MAX, int(limit or 50)))
    offset = max(0, int(offset or 0))

    stmt = select(tbl)
    term = (search or "").strip()
    if term:
        # Case-insensitive contains across the text columns only — keeps the
        # query type-safe and portable across SQLite/Postgres.
        like = f"%{term}%"
        clauses = [c.ilike(like) for c in tbl.columns if _col_py_type(c) is str]
        if clauses:
            stmt = stmt.where(or_(*clauses))

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0

    pk_cols = list(tbl.primary_key.columns)
    if pk_cols:
        stmt = stmt.order_by(*pk_cols)
    stmt = stmt.limit(limit).offset(offset)
    rows = session.execute(stmt).mappings().all()
    data = [{k: _json_safe(v) for k, v in row.items()} for row in rows]
    return {
        "name": name,
        "rows": data,
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "columns": [_col_info(c) for c in tbl.columns],
        "primary_key": [c.name for c in pk_cols],
    }


@router.post("/db/tables/{name}/rows")
def insert_db_row(
    name: str,
    payload: DbRowInsert,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    tbl = _db_table(name)
    values = _coerce_values(tbl, payload.values or {}, for_insert=True)
    if not values:
        raise HTTPException(status_code=400, detail="没有可写入的字段")
    try:
        result = session.execute(sa_insert(tbl).values(**values))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"插入失败：{exc}")
    pk = {}
    try:
        for col, val in zip(tbl.primary_key.columns, result.inserted_primary_key or []):
            pk[col.name] = _json_safe(val)
    except Exception:
        pass
    _record_audit(
        session, actor, "db_insert",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"在表 {name} 插入一行（{_pk_label(tbl, pk) if pk else '新行'}）",
    )
    return {"ok": True, "primary_key": pk}


@router.patch("/db/tables/{name}/rows")
def update_db_row(
    name: str,
    payload: DbRowUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    tbl = _db_table(name)
    where = _pk_clause(tbl, payload.pk or {})
    # Never let a primary-key column be rewritten through the values map.
    pk_names = {c.name for c in tbl.primary_key.columns}
    values = {k: v for k, v in (payload.values or {}).items() if k not in pk_names}
    coerced = _coerce_values(tbl, values, for_insert=False)
    if not coerced:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    try:
        result = session.execute(sa_update(tbl).where(where).values(**coerced))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"更新失败：{exc}")
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="未找到匹配的行")
    _record_audit(
        session, actor, "db_update",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"更新表 {name} 中的行（{_pk_label(tbl, payload.pk or {})}）",
    )
    return {"ok": True, "updated": int(result.rowcount)}


@router.post("/db/tables/{name}/rows/delete")
def delete_db_row(
    name: str,
    payload: DbRowDelete,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    tbl = _db_table(name)
    where = _pk_clause(tbl, payload.pk or {})
    try:
        result = session.execute(sa_delete(tbl).where(where))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"删除失败：{exc}")
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="未找到匹配的行")
    _record_audit(
        session, actor, "db_delete",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"删除表 {name} 中的行（{_pk_label(tbl, payload.pk or {})}）",
    )
    return {"ok": True, "deleted": int(result.rowcount)}


# ---------------------------------------------------------------------------
# Dangerous maintenance: database cleanup
#
# Wipes every user's conversation + task history and drops orphan tables that
# no model maps any more (legacy leftovers from removed features). This is
# destructive and irreversible, so on top of the owner-only gate the caller
# must re-enter an owner's account + password — a deliberate second factor so
# an unattended admin session can't trigger it by accident.
# ---------------------------------------------------------------------------


# Cleanable record sets, keyed by category. Tables are resolved from the
# models so the names track the schema even if a model is renamed. Every table
# here is per-user data; system tables (user / config / audit / device
# bindings / presence) are deliberately excluded.
_CLEANUP_CATEGORIES: dict[str, tuple] = {
    # 对话记录：消息 / 会话 / 运行记录
    "conversations": (ChatMessage, ChatSession, ChatRun),
    # 任务记录：任务作业 / 代理分发
    "tasks": (AITaskJob, AgentDispatchTask),
    # AI 互发消息 + Token 用量统计
    "ai_messages": (AIMessage, TokenUsageSnapshot),
    # 知识库与记忆：知识条目 / 记忆
    # （旧表如 evolutioninput、knowledgeembedding 会被 drop_unused_tables 自动删除）
    "knowledge": (Memory,),  # KnowledgeEntry table has been removed (knowledge now file-based under KnowledgeBase/)
    # 协作项目
    "projects": (EvolutionProject,),
}


class DbCleanupPayload(BaseModel):
    account: str
    password: str
    # Category keys from ``_CLEANUP_CATEGORIES`` whose records should be wiped.
    categories: list[str] = []
    drop_unused_tables: bool = True


@router.post("/db/cleanup")
def cleanup_database(
    payload: DbCleanupPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    account = (payload.account or "").strip()
    password = payload.password or ""
    if not account or not password:
        raise HTTPException(status_code=400, detail="请输入房主账号和密码")
    # Re-authenticate: the supplied credentials must belong to an owner. We
    # verify against any owner (not just the caller) so a co-owner can confirm,
    # but a non-owner account or a wrong password is rejected outright.
    confirm_user = session.exec(select(User).where(User.account == account)).first()
    if (
        not confirm_user
        or confirm_user.role != "owner"
        or not verify_password(password, confirm_user.hashed_password)
    ):
        raise HTTPException(status_code=403, detail="房主账号或密码不正确")

    categories = [c for c in (payload.categories or []) if c in _CLEANUP_CATEGORIES]
    unknown = [c for c in (payload.categories or []) if c not in _CLEANUP_CATEGORIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知的清理类别：{', '.join(unknown)}")
    if not (categories or payload.drop_unused_tables):
        raise HTTPException(status_code=400, detail="请至少选择一项清理内容")

    cleared: dict[str, int] = {}
    dropped: list[str] = []

    # 1) Wipe the selected record sets. None of these tables reference each
    #    other (they only point at user.id, which we leave intact), so order
    #    doesn't matter.
    models_to_clear = [m for cat in categories for m in _CLEANUP_CATEGORIES[cat]]
    for model in models_to_clear:
        tbl = model.__table__
        if tbl.name in cleared:
            continue  # guard against overlap if a model appears in two sets
        try:
            result = session.execute(sa_delete(tbl))
        except Exception as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=f"清空 {tbl.name} 失败：{exc}")
        cleared[tbl.name] = int(result.rowcount or 0)
    if models_to_clear:
        session.commit()

    # 2) Drop orphan tables — present in the live database but no longer mapped
    #    by any SQLModel model. Skip SQLite's internal bookkeeping tables.
    if payload.drop_unused_tables:
        known = set(SQLModel.metadata.tables.keys())
        try:
            live_tables = set(sa_inspect(engine).get_table_names())
        except Exception:
            logger.exception("failed to inspect live database tables")
            live_tables = set()
        orphans = sorted(
            name for name in (live_tables - known) if not name.startswith("sqlite_")
        )
        for name in orphans:
            try:
                session.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
                session.commit()
                dropped.append(name)
            except Exception:
                logger.exception(f"failed to drop orphan table {name}")
                session.rollback()

    total_deleted = sum(cleared.values())
    detail_parts = []
    if cleared:
        detail_parts.append("清空记录 " + "、".join(f"{k}×{v}" for k, v in cleared.items()))
    if dropped:
        detail_parts.append("删除无用表 " + "、".join(dropped))
    detail = "；".join(detail_parts) if detail_parts else "无可清理内容"
    logger.warning(f"owner {actor.account} ran database cleanup: {detail}")
    _record_audit(
        session, actor, "db_cleanup",
        target_type="database", target_id="cleanup", target_label="数据库清理",
        detail=detail,
    )
    return {
        "ok": True,
        "cleared": cleared,
        "dropped_tables": dropped,
        "total_deleted": total_deleted,
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit")
def list_audit(
    limit: int = 100,
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    limit = max(1, min(500, int(limit or 100)))
    rows = session.exec(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)
    ).all()
    entries = [
        {
            "id": r.id,
            "created_at": r.created_at,
            "actor_id": r.actor_id,
            "actor_account": r.actor_account,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "target_label": r.target_label,
            "detail": r.detail,
        }
        for r in rows
    ]
    return {"entries": entries}


# ---------------------------------------------------------------------------
# Auth settings (registration mode + SMTP mailer)
# ---------------------------------------------------------------------------


class AuthSettingsPayload(BaseModel):
    registration_mode: str
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    # None = 保留已存密码；空串 = 清空
    smtp_password: Optional[str] = None
    smtp_from: str = ""
    smtp_encryption: str = "ssl"


class TestEmailPayload(BaseModel):
    to: str


def _auth_settings_response(session: Session) -> dict:
    from api.services import auth_settings

    smtp = auth_settings.get_smtp_config(session)
    return {
        "registration_mode": auth_settings.get_registration_mode(session),
        "smtp": {
            "host": smtp["host"],
            "port": smtp["port"],
            "username": smtp["username"],
            "from_addr": smtp["from_addr"],
            "encryption": smtp["encryption"],
            # 密码永不回传，只回传是否已配置。
            "password_set": bool(smtp["password"]),
        },
        "email_enabled": auth_settings.smtp_configured(session),
    }


@router.get("/auth-settings")
def get_auth_settings(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    return _auth_settings_response(session)


@router.put("/auth-settings")
def update_auth_settings(
    payload: AuthSettingsPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    from api.services import auth_settings

    mode = (payload.registration_mode or "").strip().lower()
    if mode not in auth_settings.REGISTRATION_MODES:
        raise HTTPException(status_code=400, detail="无效的注册模式")
    encryption = (payload.smtp_encryption or "ssl").strip().lower()
    if encryption not in auth_settings.SMTP_ENCRYPTIONS:
        raise HTTPException(status_code=400, detail="无效的加密方式")

    auth_settings.set_registration_mode(session, mode)
    auth_settings.save_smtp_config(
        session,
        host=payload.smtp_host or "",
        port=payload.smtp_port or 465,
        username=payload.smtp_username or "",
        password=payload.smtp_password,
        from_addr=payload.smtp_from or "",
        encryption=encryption,
    )
    session.commit()

    if mode == "email" and not auth_settings.smtp_configured(session):
        # 不阻断保存，但明确提醒：邮箱注册模式 + 未配置 SMTP 会导致没人能注册。
        note = "注意：当前未配置 SMTP，邮箱验证注册将不可用"
    else:
        note = ""

    _record_audit(
        session, actor, "update_auth_settings",
        target_type="settings", target_id="auth", target_label="注册与邮箱设置",
        detail=f"注册模式「{mode}」，SMTP {payload.smtp_host or '(未设置)'}:{payload.smtp_port}",
    )
    result = _auth_settings_response(session)
    if note:
        result["note"] = note
    return result


@router.post("/auth-settings/test-email")
def send_test_email(
    payload: TestEmailPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    from api.services import auth_settings, email_service

    to = auth_settings.normalize_email(payload.to)
    if not auth_settings.is_valid_email(to):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    try:
        email_service.send_email(
            session,
            to,
            "HeySure 邮件服务测试",
            "这是一封来自 HeySure 管理控制台的测试邮件。\n\n"
            "收到本邮件说明 SMTP 配置正确，邮箱验证码注册 / 登录可以正常工作。\n\n"
            "—— HeySure · 数字社会操作系统",
        )
    except email_service.EmailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    _record_audit(
        session, actor, "send_test_email",
        target_type="settings", target_id="auth", target_label=to,
        detail=f"发送测试邮件至 {to}",
    )
    return {"ok": True}
