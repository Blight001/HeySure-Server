"""MCP facade for member-scoped sendable file references."""

import os
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session

from api.database import engine
from api.models import ChatMessageMedia
from api.services.storage.workspace_files import (
    list_file_refs,
    register_workspace_file,
    resolve_file_ref,
    resolve_workspace_path,
    save_workspace_bytes,
    unregister_file_ref,
)
from api.services.storage.temporary_file_links import (
    configured_public_base_url,
    create_temporary_file_link,
    revoke_temporary_file_link,
)


WORKSPACE_FILE_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["register", "import_chat_media", "view_image", "info", "list", "unregister", "create_temporary_link", "revoke_temporary_link"],
            "description": "register 注册工作区文件；view_image 将工作区图片安全附加到下一轮模型输入；create_temporary_link 创建默认 5 分钟的公网临时下载链接；revoke_temporary_link 提前撤销；info/list 查看引用；unregister 仅删除引用、不删除原文件。",
        },
        "workspace_path": {
            "type": "string",
            "description": "register 必填。相对当前 AI 工作区的文件路径；拒绝绝对路径和越界路径。",
        },
        "file_ref": {"type": "string", "description": "view_image/info/unregister 可用，格式 file_...。"},
        "grant_id": {"type": "string", "description": "revoke_temporary_link 必填，格式 fgrant_...。"},
        "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 900, "description": "临时链接有效期，默认 300 秒，最长 900 秒。"},
        "file_name": {"type": "string", "description": "register 可选，对外发送时显示的文件名。"},
        "media_id": {"type": "integer", "description": "import_chat_media 必填，取自 /api/chat/media/{media_id}/{token}。"},
        "media_token": {"type": "string", "description": "import_chat_media 必填，取自同一对话媒体 URL。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "list 返回数量，默认 50。"},
    },
    "required": ["action"],
}

MAX_MODEL_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_SIGNATURES = (
    ("image/png", lambda head: head.startswith(b"\x89PNG\r\n\x1a\n")),
    ("image/jpeg", lambda head: head.startswith(b"\xff\xd8\xff")),
    ("image/gif", lambda head: head.startswith((b"GIF87a", b"GIF89a"))),
    ("image/webp", lambda head: head.startswith(b"RIFF") and head[8:12] == b"WEBP"),
)


def _with_send_example(result: Dict[str, Any]) -> Dict[str, Any]:
    result["send_example"] = {
        "tool": "message.send+to",
        "arguments": {"to": "user", "attachments": [{"file_ref": result["file_ref"]}]},
    }
    return result


def _import_chat_media(
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        media_id = int(args.get("media_id") or 0)
    except (TypeError, ValueError):
        media_id = 0
    token = str(args.get("media_token") or args.get("token") or "").strip()
    if media_id <= 0 or not token:
        raise HTTPException(status_code=400, detail="media_id and media_token are required")
    with Session(engine) as session:
        row = session.get(ChatMessageMedia, media_id)
        if row is None or int(row.user_id) != int(user_id):
            raise HTTPException(status_code=404, detail={"code": "FILE_NOT_FOUND", "message": "chat media not found"})
        import secrets

        if not secrets.compare_digest(str(row.token or ""), token):
            raise HTTPException(status_code=404, detail={"code": "FILE_NOT_FOUND", "message": "chat media not found"})
        mime_type = str(row.media_type or "application/octet-stream")
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime_type, ".bin")
        result = save_workspace_bytes(
            user_id=user_id,
            ai_config_id=ai_config_id,
            data=row.data,
            file_name=str(args.get("file_name") or f"chat_media_{media_id}{extension}"),
        )
    return _with_send_example(result)


def _detected_image_mime(server_path: str) -> str:
    with open(server_path, "rb") as handle:
        head = handle.read(16)
    return next((mime for mime, matches in _IMAGE_SIGNATURES if matches(head)), "")


def _view_workspace_image(
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
) -> Dict[str, Any]:
    file_ref = str(args.get("file_ref") or "").strip()
    if file_ref:
        record = resolve_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=file_ref,
        )
    else:
        workspace_path = str(args.get("workspace_path") or "").strip()
        if not workspace_path:
            raise HTTPException(status_code=400, detail="view_image requires file_ref or workspace_path")
        server_path = resolve_workspace_path(user_id, ai_config_id, workspace_path)
        size = os.stat(server_path).st_size
        if size > MAX_MODEL_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail={"code": "IMAGE_TOO_LARGE", "message": "model image input cannot exceed 10 MB"})
        if not _detected_image_mime(server_path):
            raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_IMAGE", "message": "only PNG, JPEG, WebP, and GIF images can be viewed"})
        registered = register_workspace_file(
            user_id=user_id,
            ai_config_id=ai_config_id,
            workspace_path=workspace_path,
        )
        record = resolve_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=registered["file_ref"],
        )
    if int(record.get("bytes") or 0) > MAX_MODEL_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail={"code": "IMAGE_TOO_LARGE", "message": "model image input cannot exceed 10 MB"})
    detected_mime = _detected_image_mime(record["server_path"])
    if not detected_mime:
        raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_IMAGE", "message": "only PNG, JPEG, WebP, and GIF images can be viewed"})
    return {
        **record,
        "mime_type": detected_mime,
        "success": True,
        "_heysure_model_image": True,
    }


def _workspace_file_manage(
    user_id: int,
    args: Dict[str, Any],
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    action = str((args or {}).get("action") or "").strip().lower()
    if action == "register":
        result = register_workspace_file(
            user_id=user_id,
            ai_config_id=ai_config_id,
            workspace_path=str(args.get("workspace_path") or ""),
            file_name=str(args.get("file_name") or ""),
        )
        return _with_send_example(result)
    if action == "import_chat_media":
        return _import_chat_media(user_id, ai_config_id, args)
    if action == "view_image":
        return _view_workspace_image(user_id, ai_config_id, args)
    if action == "info":
        result = resolve_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=str(args.get("file_ref") or ""),
        )
        result.pop("server_path", None)
        return result
    if action == "list":
        return list_file_refs(user_id=user_id, ai_config_id=ai_config_id, limit=int(args.get("limit") or 50))
    if action == "unregister":
        return unregister_file_ref(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=str(args.get("file_ref") or ""),
        )
    if action == "create_temporary_link":
        if not ai_config_id:
            raise HTTPException(status_code=400, detail="ai_config_id is required for temporary links")
        return create_temporary_file_link(
            user_id=user_id,
            ai_config_id=ai_config_id,
            file_ref=str(args.get("file_ref") or ""),
            ttl_seconds=args.get("ttl_seconds") or 300,
            public_base_url=configured_public_base_url(),
        )
    if action == "revoke_temporary_link":
        if not ai_config_id:
            raise HTTPException(status_code=400, detail="ai_config_id is required for temporary links")
        return revoke_temporary_file_link(
            user_id=user_id,
            ai_config_id=ai_config_id,
            grant_id=str(args.get("grant_id") or ""),
        )
    raise HTTPException(status_code=400, detail="unsupported workspace file action")
