"""MCP facade for member-scoped sendable file references."""

from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session

from api.database import engine
from api.models import ChatMessageMedia
from api.services.storage.workspace_files import (
    list_file_refs,
    register_workspace_file,
    resolve_file_ref,
    save_workspace_bytes,
    unregister_file_ref,
)


WORKSPACE_FILE_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["register", "import_chat_media", "info", "list", "unregister"],
            "description": "register 注册工作区文件；import_chat_media 把对话图片保存到工作区；info/list 查看引用；unregister 仅删除引用、不删除原文件。",
        },
        "workspace_path": {
            "type": "string",
            "description": "register 必填。相对当前 AI 工作区的文件路径；拒绝绝对路径和越界路径。",
        },
        "file_ref": {"type": "string", "description": "info/unregister 必填，格式 file_...。"},
        "file_name": {"type": "string", "description": "register 可选，对外发送时显示的文件名。"},
        "media_id": {"type": "integer", "description": "import_chat_media 必填，取自 /api/chat/media/{media_id}/{token}。"},
        "media_token": {"type": "string", "description": "import_chat_media 必填，取自同一对话媒体 URL。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "list 返回数量，默认 50。"},
    },
    "required": ["action"],
}


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
    raise HTTPException(status_code=400, detail="action must be register, import_chat_media, info, list, or unregister")
