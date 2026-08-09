"""Generic admin database browser and row-edit routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, insert, or_, update
from sqlmodel import SQLModel, Session, select

from api.database import get_session
from api.models import User
from gateway.routers.admin import (
    _record_audit,
    require_admin_user,
    require_owner_user,
)


router = APIRouter()
PREFIX = "/api/admin"
DB_PAGE_MAX = 200


class DbRowInsert(BaseModel):
    values: dict


class DbRowUpdate(BaseModel):
    pk: dict
    values: dict


class DbRowDelete(BaseModel):
    pk: dict


def db_table(name: str):
    table = SQLModel.metadata.tables.get(name)
    if table is None:
        raise HTTPException(status_code=404, detail="数据表不存在")
    return table


def column_python_type(column) -> type:
    try:
        return column.type.python_type
    except Exception:
        return str


def column_info(column) -> dict:
    return {
        "name": column.name,
        "type": str(column.type),
        "py_type": column_python_type(column).__name__,
        "nullable": bool(column.nullable),
        "primary_key": bool(column.primary_key),
    }


def json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def coerce_value(column, raw):
    if raw is None:
        return None
    python_type = column_python_type(column)
    if not isinstance(raw, str):
        return raw
    if python_type is str:
        return raw
    if raw == "":
        return None
    if python_type is bool:
        return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    if python_type is int:
        return _coerce_number(column.name, raw, int, "整数")
    if python_type is float:
        return _coerce_number(column.name, raw, float, "数字")
    return raw


def _coerce_number(name: str, raw: str, converter, label: str):
    try:
        return converter(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"字段 {name} 需要{label}") from exc


def coerce_values(table, values: dict, *, for_insert: bool) -> dict:
    columns = {column.name: column for column in table.columns}
    result = {}
    for key, raw in values.items():
        column = columns.get(key)
        if column is None:
            continue
        if (
            for_insert
            and column.primary_key
            and column.autoincrement
            and raw in (None, "")
        ):
            continue
        result[key] = coerce_value(column, raw)
    return result


def primary_key_clause(table, primary_key: dict):
    columns = list(table.primary_key.columns)
    if not columns:
        raise HTTPException(status_code=400, detail="该表没有主键，无法定位行")
    clauses = []
    for column in columns:
        if column.name not in primary_key:
            raise HTTPException(status_code=400, detail=f"缺少主键字段 {column.name}")
        clauses.append(column == coerce_value(column, primary_key[column.name]))
    return and_(*clauses)


def primary_key_label(table, values: dict) -> str:
    names = [column.name for column in table.primary_key.columns]
    return ", ".join(f"{name}={values.get(name)}" for name in names) or "?"


@router.get("/db/tables")
def list_db_tables(
    session: Session = Depends(get_session),
    _admin: User = Depends(require_admin_user),
) -> dict:
    tables = []
    for table in SQLModel.metadata.sorted_tables:
        try:
            count = session.execute(select(func.count()).select_from(table)).scalar()
        except Exception:
            count = -1
        tables.append(
            {
                "name": table.name,
                "row_count": int(count or 0) if count is not None and count >= 0 else -1,
                "columns": [column_info(column) for column in table.columns],
                "primary_key": [column.name for column in table.primary_key.columns],
            }
        )
    tables.sort(key=lambda item: item["name"])
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
    table = db_table(name)
    limit = max(1, min(DB_PAGE_MAX, int(limit or 50)))
    offset = max(0, int(offset or 0))
    statement = select(table)
    term = (search or "").strip()
    if term:
        like = f"%{term}%"
        clauses = [
            column.ilike(like)
            for column in table.columns
            if column_python_type(column) is str
        ]
        if clauses:
            statement = statement.where(or_(*clauses))
    total = (
        session.execute(select(func.count()).select_from(statement.subquery())).scalar()
        or 0
    )
    primary_keys = list(table.primary_key.columns)
    if primary_keys:
        statement = statement.order_by(*primary_keys)
    rows = session.execute(statement.limit(limit).offset(offset)).mappings().all()
    return {
        "name": name,
        "rows": [
            {key: json_safe(value) for key, value in row.items()} for row in rows
        ],
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "columns": [column_info(column) for column in table.columns],
        "primary_key": [column.name for column in primary_keys],
    }


@router.post("/db/tables/{name}/rows")
def insert_db_row(
    name: str,
    payload: DbRowInsert,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    table = db_table(name)
    values = coerce_values(table, payload.values or {}, for_insert=True)
    if not values:
        raise HTTPException(status_code=400, detail="没有可写入的字段")
    try:
        result = session.execute(insert(table).values(**values))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"插入失败：{exc}") from exc
    primary_key = _inserted_primary_key(table, result)
    _record_audit(
        session, actor, "db_insert",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"在表 {name} 插入一行（{primary_key_label(table, primary_key) if primary_key else '新行'}）",
    )
    return {"ok": True, "primary_key": primary_key}


def _inserted_primary_key(table, result) -> dict:
    try:
        return {
            column.name: json_safe(value)
            for column, value in zip(
                table.primary_key.columns, result.inserted_primary_key or []
            )
        }
    except Exception:
        return {}


@router.patch("/db/tables/{name}/rows")
def update_db_row(
    name: str,
    payload: DbRowUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    table = db_table(name)
    where = primary_key_clause(table, payload.pk or {})
    primary_key_names = {column.name for column in table.primary_key.columns}
    values = {
        key: value
        for key, value in (payload.values or {}).items()
        if key not in primary_key_names
    }
    coerced = coerce_values(table, values, for_insert=False)
    if not coerced:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    try:
        result = session.execute(update(table).where(where).values(**coerced))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"更新失败：{exc}") from exc
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="未找到匹配的行")
    _record_audit(
        session, actor, "db_update",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"更新表 {name} 中的行（{primary_key_label(table, payload.pk or {})}）",
    )
    return {"ok": True, "updated": int(result.rowcount)}


@router.post("/db/tables/{name}/rows/delete")
def delete_db_row(
    name: str,
    payload: DbRowDelete,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner_user),
) -> dict:
    table = db_table(name)
    where = primary_key_clause(table, payload.pk or {})
    try:
        result = session.execute(delete(table).where(where))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"删除失败：{exc}") from exc
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="未找到匹配的行")
    _record_audit(
        session, actor, "db_delete",
        target_type="db_row", target_id=name, target_label=name,
        detail=f"删除表 {name} 中的行（{primary_key_label(table, payload.pk or {})}）",
    )
    return {"ok": True, "deleted": int(result.rowcount)}
