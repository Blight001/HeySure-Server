"""Admin panel API — service monitoring + user management.

All routes are gated to platform staff (``owner`` / ``admin``). The owner
(房主) is the only tier that can change roles or touch another owner; admins
(管理员) can monitor services, restart sub-tasks, list members and reset
member passwords.

Mounted at ``/api/admin`` (see ``PREFIX``) and auto-discovered by
``gateway.app``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
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

from api.auth import verify_password
from api.database import engine, get_session
from api.models import (
    AdminAuditLog,
    AgentDispatchTask,
    AIMessage,
    AITaskJob,
    ChatMessage,
    ChatMessageMedia,
    ChatRun,
    ChatSession,
    EvolutionProject,
    Memory,
    TokenUsageSnapshot,
    User,
)
from gateway.routers.auth import get_current_user


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
# Database export / import (full backup & restore)
#
# Export dumps every mapped table to a single JSON document the owner can
# download — a complete snapshot of all user data (accounts, chats, AI
# configs, tasks, knowledge, devices, …). Import restores such a snapshot,
# replacing the contents of every table it carries. Both are owner-only;
# import additionally re-confirms an owner's password (a deliberate second
# factor) because it wipes and rewrites the database.
#
# Serialization is schema-agnostic — tables and columns are discovered from
# SQLModel's metadata, so new models are covered automatically. The single
# binary column (chat media blobs) is base64-wrapped as ``{"__b64__": "..."}``
# so the document stays valid JSON and round-trips losslessly.
# ---------------------------------------------------------------------------


EXPORT_VERSION = 1
_MEDIA_TABLE = ChatMessageMedia.__table__.name


def _export_value(v):
    """Make a DB value JSON-safe while keeping it reversible on import."""
    if isinstance(v, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)  # no datetime/Decimal columns today; stringify as a fallback


def _import_value(col, v):
    """Reverse :func:`_export_value` for a single column value."""
    if isinstance(v, dict) and "__b64__" in v:
        return base64.b64decode(v["__b64__"])
    return v


@router.get("/db/export")
def export_database(
    include_media: bool = True,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
):
    """Stream the whole database as a downloadable JSON backup.

    Rows are streamed table by table over a server-side cursor so a large
    export — chat media in particular — never has to be buffered in memory all
    at once. Pass ``include_media=false`` to skip the binary blob table for a
    much smaller dump.
    """
    tables = [
        t for t in SQLModel.metadata.sorted_tables
        if include_media or t.name != _MEDIA_TABLE
    ]
    _record_audit(
        session, actor, "db_export",
        target_type="database", target_id="export", target_label="数据库导出",
        detail=f"导出 {len(tables)} 张表" + ("" if include_media else "（不含媒体文件）"),
    )

    def generate():
        # Use a dedicated connection: the request session may already be closed
        # by the time this generator runs (StreamingResponse consumes it after
        # the endpoint returns).
        with engine.connect() as conn:
            yield (
                '{"heysure_export": true, '
                f'"version": {EXPORT_VERSION}, '
                f'"exported_at": {json.dumps(time.time())}, '
                '"tables": {'
            )
            for ti, tbl in enumerate(tables):
                yield ("," if ti else "") + json.dumps(tbl.name) + ":["
                result = conn.execution_options(stream_results=True).execute(select(tbl))
                for ri, row in enumerate(result.mappings()):
                    obj = {k: _export_value(v) for k, v in row.items()}
                    yield ("," if ri else "") + json.dumps(obj, ensure_ascii=False)
                yield "]"
            yield "}}"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    headers = {"Content-Disposition": f'attachment; filename="heysure-backup-{stamp}.json"'}
    return StreamingResponse(generate(), media_type="application/json", headers=headers)


def _reset_sequences(session: Session) -> None:
    """Advance every serial/identity sequence past the largest imported id.

    Bulk-inserting rows with explicit primary keys leaves Postgres' backing
    sequences untouched, so the next natural insert would collide. Realign
    each one to ``MAX(id)`` (or 1 for an empty table).
    """
    for tbl in SQLModel.metadata.sorted_tables:
        for col in tbl.columns:
            seq = session.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": tbl.name, "c": col.name},
            ).scalar()
            if not seq:
                continue  # column has no serial sequence behind it
            session.execute(
                text(
                    f'SELECT setval(:seq, GREATEST(COALESCE((SELECT MAX("{col.name}") '
                    f'FROM "{tbl.name}"), 0), 1))'
                ),
                {"seq": seq},
            )


@router.post("/db/import")
def import_database(
    file: UploadFile = File(...),
    account: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    """Restore a backup produced by :func:`export_database`.

    Every table carried by the document is wiped and repopulated inside one
    transaction, so a failure rolls back to the pre-import state. Tables the
    document does not mention are left untouched. Requires an owner's password
    as a second factor — the same gate the cleanup endpoint uses.
    """
    account = (account or "").strip()
    if not account or not password:
        raise HTTPException(status_code=400, detail="请输入房主账号和密码")
    # Re-authenticate against any owner account (a co-owner may confirm).
    confirm_user = session.exec(select(User).where(User.account == account)).first()
    if (
        not confirm_user
        or confirm_user.role != "owner"
        or not verify_password(password, confirm_user.hashed_password)
    ):
        raise HTTPException(status_code=403, detail="房主账号或密码不正确")

    try:
        raw = file.file.read()
        doc = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无法解析备份文件：{exc}")
    if not isinstance(doc, dict) or not doc.get("heysure_export"):
        raise HTTPException(status_code=400, detail="不是有效的 HeySure 备份文件")
    payload_tables = doc.get("tables")
    if not isinstance(payload_tables, dict):
        raise HTTPException(status_code=400, detail="备份文件缺少 tables 字段")

    # Only operate on tables the current schema still knows about; surface the
    # rest so an out-of-date backup is obvious instead of silently dropped.
    known = {t.name for t in SQLModel.metadata.sorted_tables}
    skipped = sorted(set(payload_tables) - known)

    # Capture the actor's identity now: the import may wipe and replace the
    # ``user`` table, after which the ORM object is expired and reloading it
    # could fail (the row may no longer exist in the restored data).
    actor_id, actor_account = actor.id, actor.account

    imported: dict[str, int] = {}
    try:
        # 1) Wipe child-first so foreign keys never block a delete.
        for tbl in reversed(SQLModel.metadata.sorted_tables):
            if tbl.name in payload_tables:
                session.execute(sa_delete(tbl))
        # 2) Insert parent-first so foreign keys are always satisfiable.
        for tbl in SQLModel.metadata.sorted_tables:
            rows = payload_tables.get(tbl.name)
            if not isinstance(rows, list) or not rows:
                continue
            cols = {c.name: c for c in tbl.columns}
            batch = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                clean = {k: _import_value(cols[k], v) for k, v in row.items() if k in cols}
                if clean:
                    batch.append(clean)
            if batch:
                session.execute(sa_insert(tbl), batch)
                imported[tbl.name] = len(batch)
        _reset_sequences(session)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"导入失败，已回滚到导入前状态：{exc}")

    total = sum(imported.values())
    logger.warning(
        f"owner {actor_account} imported a database backup: "
        f"{total} rows across {len(imported)} tables"
    )
    # Build the audit row from the captured identity (the actor ORM object is
    # expired after the restore). ``actor_id`` is not a foreign key, so it is
    # safe even if that account no longer exists in the imported data.
    detail = (
        f"导入 {len(imported)} 张表共 {total} 行"
        + (f"；跳过未知表 {', '.join(skipped)}" if skipped else "")
    )
    try:
        session.add(
            AdminAuditLog(
                actor_id=actor_id,
                actor_account=actor_account,
                action="db_import",
                target_type="database",
                target_id="import",
                target_label="数据库导入",
                detail=detail,
            )
        )
        session.commit()
    except Exception:
        logger.exception("failed to write admin audit log for db_import")
        session.rollback()
    return {"ok": True, "imported": imported, "total": total, "skipped_tables": skipped}


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
    from api.services.access import auth_settings

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
    from api.services.access import auth_settings

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
    from api.services.access import auth_settings
    from api.services import email_service

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


# ---------------------------------------------------------------------------
# Remote-control ICE settings (STUN + TURN)
#
# One server-side source of truth for the ICE servers every remote-control peer
# uses (web console, game viewer, desktop / browser / mobile agents). Without a
# TURN relay the session is STUN-only and cannot traverse symmetric NAT, so the
# feature dies on many real deployments. Configuring TURN here (or via
# HEYSURE_TURN_* env) fixes it for all clients at once.
# ---------------------------------------------------------------------------


class RtcSettingsPayload(BaseModel):
    stun_url: str = ""
    turn_url: str = ""
    turn_username: str = ""
    # None = 保留已存密码；空串 = 清空
    turn_password: Optional[str] = None


def _rtc_settings_response(session: Session) -> dict:
    from api.services.access import ice_settings

    cfg = ice_settings.get_ice_config(session)
    return {
        "stun_url": cfg["stun_url"],
        "turn_url": cfg["turn_url"],
        "turn_username": cfg["turn_username"],
        # 密码永不回传，只回传是否已配置。
        "turn_password_set": bool(cfg["turn_password"]),
        "turn_enabled": bool(str(cfg["turn_url"]).strip()),
        # Preview of exactly what clients receive, minus the credential.
        "ice_servers": [
            {k: v for k, v in server.items() if k != "credential"}
            for server in ice_settings.build_ice_servers(session)
        ],
    }


@router.get("/rtc-settings")
def get_rtc_settings(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    return _rtc_settings_response(session)


@router.put("/rtc-settings")
def update_rtc_settings(
    payload: RtcSettingsPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    from api.services.access import ice_settings

    ice_settings.save_ice_config(
        session,
        stun_url=payload.stun_url or "",
        turn_url=payload.turn_url or "",
        turn_username=payload.turn_username or "",
        turn_password=payload.turn_password,
    )
    session.commit()
    _record_audit(
        session, actor, "update_rtc_settings",
        target_type="settings", target_id="rtc", target_label="远程控制 ICE 设置",
        detail=f"STUN「{payload.stun_url or '(未设置)'}」，TURN「{payload.turn_url or '(未设置)'}」",
    )
    return _rtc_settings_response(session)
