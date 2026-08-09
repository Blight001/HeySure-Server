"""Sandboxed admin file-manager routes for the server data directory."""

import mimetypes
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session

from api.core.config import DATA_DIR
from api.database import get_session
from api.models import User
from gateway.routers.admin import _record_audit, require_admin_user


router = APIRouter()
PREFIX = "/api/admin"
DATA_ROOT = os.path.realpath(DATA_DIR)
MAX_EDIT_BYTES = 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".html", ".htm", ".css", ".scss",
    ".bat", ".cmd", ".ps1", ".sh", ".sql",
}


class FileWritePayload(BaseModel):
    path: str
    content: str = ""


class FilePathPayload(BaseModel):
    path: str


class FileRenamePayload(BaseModel):
    path: str
    new_path: str


class FileBatchPayload(BaseModel):
    paths: list[str]


def safe_data_path(relative: str) -> str:
    relative = (relative or "").strip().replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(DATA_ROOT, relative))
    if full != DATA_ROOT and not full.startswith(DATA_ROOT + os.sep):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    return full


def relative_to_root(full: str) -> str:
    relative = os.path.relpath(full, DATA_ROOT).replace(os.sep, "/")
    return "" if relative == "." else relative


def file_kind(name: str) -> str:
    extension = os.path.splitext(name)[1].lower()
    if extension in IMAGE_EXTS:
        return "image"
    if extension in TEXT_EXTS:
        return "text"
    mime, _ = mimetypes.guess_type(name)
    if mime and (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/javascript"}
    ):
        return "text"
    return "text" if not extension else "binary"


def _entry_info(full: str) -> dict:
    stat = os.stat(full)
    is_dir = os.path.isdir(full)
    return {
        "name": os.path.basename(full),
        "path": relative_to_root(full),
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "modified": stat.st_mtime,
        "kind": "dir" if is_dir else file_kind(os.path.basename(full)),
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
    full = safe_data_path(path)
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
            continue
    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    return {"path": relative_to_root(full), "entries": entries}


@router.get("/files/read")
def read_file(path: str, _admin: User = Depends(require_admin_user)) -> dict:
    full = safe_data_path(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = os.path.getsize(full)
    if size > MAX_EDIT_BYTES:
        return _read_payload(full, size, too_large=True)
    with open(full, "rb") as handle:
        data = handle.read()
    if not _is_probably_text(data):
        return _read_payload(full, size, binary=True)
    return {
        **_read_payload(full, size),
        "content": data.decode("utf-8"),
        "kind": file_kind(os.path.basename(full)),
    }


def _read_payload(
    full: str,
    size: int,
    *,
    binary: bool = False,
    too_large: bool = False,
) -> dict:
    return {
        "path": relative_to_root(full),
        "size": size,
        "binary": binary,
        "too_large": too_large,
        "content": "",
    }


@router.get("/files/raw")
def raw_file(path: str, _admin: User = Depends(require_admin_user)):
    full = safe_data_path(path)
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
    full = safe_data_path(payload.path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if os.path.isdir(full):
        raise HTTPException(status_code=400, detail="目标是文件夹，无法写入")
    if len(payload.content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise HTTPException(status_code=400, detail="文件内容过大")
    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as handle:
        handle.write(payload.content)
    relative = relative_to_root(full)
    _record_audit(
        session,
        actor,
        "file_write",
        target_type="file",
        target_id=relative,
        target_label=relative,
        detail=f"{'修改' if existed else '新建'}文件 data/{relative}",
    )
    return {"ok": True, "path": relative, "created": not existed}


@router.post("/files/mkdir")
def make_dir(
    payload: FilePathPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    full = safe_data_path(payload.path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件夹路径")
    if os.path.exists(full):
        raise HTTPException(status_code=400, detail="该路径已存在")
    os.makedirs(full, exist_ok=False)
    relative = relative_to_root(full)
    _record_audit(
        session, actor, "file_mkdir",
        target_type="file", target_id=relative, target_label=relative,
        detail=f"新建文件夹 data/{relative}",
    )
    return {"ok": True, "path": relative}


@router.post("/files/rename")
def rename_path(
    payload: FileRenamePayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    source = safe_data_path(payload.path)
    destination = safe_data_path(payload.new_path)
    if source == DATA_ROOT or destination == DATA_ROOT:
        raise HTTPException(status_code=400, detail="非法的文件路径")
    if not os.path.exists(source):
        raise HTTPException(status_code=404, detail="源文件不存在")
    if os.path.exists(destination):
        raise HTTPException(status_code=400, detail="目标已存在")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    os.rename(source, destination)
    source_relative = relative_to_root(source)
    destination_relative = relative_to_root(destination)
    _record_audit(
        session, actor, "file_rename",
        target_type="file", target_id=destination_relative,
        target_label=destination_relative,
        detail=f"重命名 data/{source_relative} → data/{destination_relative}",
    )
    return {"ok": True, "path": destination_relative}


@router.delete("/files")
def delete_path(
    path: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    full = safe_data_path(path)
    if full == DATA_ROOT:
        raise HTTPException(status_code=400, detail="不能删除数据根目录")
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="路径不存在")
    relative = relative_to_root(full)
    is_dir = os.path.isdir(full)
    _delete_file_or_directory(full, is_dir)
    _record_audit(
        session, actor, "file_delete",
        target_type="file", target_id=relative, target_label=relative,
        detail=f"删除{'文件夹' if is_dir else '文件'} data/{relative}",
    )
    return {"ok": True, "path": relative}


def _delete_file_or_directory(full: str, is_dir: bool) -> None:
    if is_dir:
        shutil.rmtree(full, ignore_errors=True)
    else:
        os.remove(full)


@router.post("/files/batch-delete")
def batch_delete_paths(
    payload: FileBatchPayload,
    session: Session = Depends(get_session),
    actor: User = Depends(require_admin_user),
) -> dict:
    deleted = []
    errors = []
    for raw in payload.paths or []:
        try:
            full = safe_data_path(raw)
            if full == DATA_ROOT:
                raise ValueError("不能删除数据根目录")
            if not os.path.exists(full):
                raise ValueError("路径不存在")
            _delete_file_or_directory(full, os.path.isdir(full))
            deleted.append(relative_to_root(full))
        except Exception as exc:
            errors.append({"path": raw, "error": str(exc)})
    if deleted:
        _record_batch_delete(session, actor, deleted)
    return {"ok": True, "deleted": deleted, "errors": errors}


def _record_batch_delete(session: Session, actor: User, deleted: list[str]) -> None:
    detail = f"批量删除 {len(deleted)} 项：" + "、".join(
        f"data/{path}" for path in deleted[:10]
    )
    if len(deleted) > 10:
        detail += "…"
    _record_audit(
        session, actor, "file_delete",
        target_type="file", target_id=";".join(deleted[:20]),
        target_label=f"{len(deleted)} 项", detail=detail,
    )
