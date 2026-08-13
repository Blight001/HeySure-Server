import json
import mimetypes
import os
from typing import Any, Dict, Optional

import requests
from fastapi import HTTPException

from api.integrations.media_source import MediaSource, infer_media_kind, resolve_media_source
from api.models import AssistantAIConfig
from api.database import engine
from api.services.bot_directory import config_view_for_connection, resolve_connection
from sqlmodel import Session
from ..text_format import strip_markdown_to_plain
from ..transport import TokenCache, load_active_config, parse_json_response
from ._config import read_feishu_config

FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE = TokenCache()


def normalize_feishu_text(text: str, *, strip_markdown: bool = True) -> str:
    """Convert common Markdown punctuation into plain Feishu text.

    Feishu text messages do not need Markdown syntax. Keep the readable
    content while removing formatting markers that otherwise leak to users.
    """
    if not strip_markdown:
        return str(text or "").strip()
    return strip_markdown_to_plain(text, collapse_tables=True)


def _validate_feishu_credentials(bot_cfg: Dict[str, Any]) -> None:
    if not bot_cfg.get("webhook_url") and (not bot_cfg.get("app_id") or not bot_cfg.get("app_secret")):
        raise HTTPException(status_code=400, detail="Feishu 仅通知 URL 或 app_id/app_secret 未配置")


def _load_feishu_config(user_id: int, ai_config_id: Optional[int], connection_ref: str = "") -> AssistantAIConfig:
    if connection_ref and ai_config_id:
        from ._config import FEISHU_DEFAULTS
        with Session(engine) as session:
            cfg = session.get(AssistantAIConfig, int(ai_config_id))
            row = resolve_connection(
                session, user_id=user_id, ai_config_id=int(ai_config_id),
                channel="feishu", connection_ref=connection_ref,
            )
        if cfg is None or int(cfg.user_id) != int(user_id) or row is None:
            raise HTTPException(status_code=404, detail="Feishu connection not found")
        cfg = config_view_for_connection(cfg, row, FEISHU_DEFAULTS)
        _validate_feishu_credentials(read_feishu_config(cfg))
        return cfg
    return load_active_config(
        user_id,
        ai_config_id,
        channel="feishu",
        tool_name="feishu",
        channel_label="Feishu",
        read_config=read_feishu_config,
        validate_credentials=_validate_feishu_credentials,
    )


def get_tenant_access_token(user_id: int, ai_config_id: Optional[int], connection_ref: str = "") -> str:
    cfg = _load_feishu_config(user_id, ai_config_id, connection_ref)
    bot_cfg = read_feishu_config(cfg)

    def _fetch() -> "tuple[str, int]":
        res = requests.post(
            f"{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"app_id": bot_cfg.get("app_id", ""), "app_secret": bot_cfg.get("app_secret", "")},
            timeout=20,
        )
        data = _feishu_json(res)
        _raise_feishu_error(res, data, "token")
        token = str(data.get("tenant_access_token") or "").strip()
        if not token:
            raise HTTPException(status_code=502, detail="Feishu token response missing tenant_access_token")
        return token, int(data.get("expire") or 7200)

    return _TOKEN_CACHE.get_or_fetch(connection_ref or int(cfg.id or 0), _fetch)


# --------------------------------------------------------------------------- #
# Shared send-path helpers — the JSON parsing, open-API error check, target
# resolution/validation and result shaping that every Feishu send repeated.
# --------------------------------------------------------------------------- #

_FEISHU_RECEIVE_ID_TYPES = {"chat_id", "open_id", "user_id", "union_id", "email"}


def _feishu_json(res: requests.Response) -> Dict[str, Any]:
    return parse_json_response(res)


def _raise_feishu_error(res: requests.Response, data: Dict[str, Any], action: str) -> None:
    """Raise a 502 with the open-platform error body when the call failed."""
    if not res.ok or int((data or {}).get("code") or 0) != 0:
        raise HTTPException(status_code=502, detail=f"Feishu {action} failed: {data or res.text}")


def _resolve_feishu_target(bot_cfg: Dict[str, Any], receive_id: str, receive_id_type: str) -> tuple[str, str]:
    """Apply the default-receiver fallback (no validation), once."""
    target_id = str(receive_id or bot_cfg.get("default_receive_id") or "").strip()
    target_type = str(receive_id_type or bot_cfg.get("default_receive_id_type") or "chat_id").strip()
    return target_id, target_type


def _validate_feishu_target(target_id: str, target_type: str) -> None:
    if not target_id:
        raise HTTPException(status_code=400, detail="Feishu receive_id is required")
    if target_type not in _FEISHU_RECEIVE_ID_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported Feishu receive_id_type: {target_type}")


def _feishu_send_result(receive_id: str, receive_id_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "message_id": (data.get("data") or {}).get("message_id"),
        "raw": data,
    }


def send_feishu_text_message(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    text: str,
    receive_id: str = "",
    receive_id_type: str = "",
    connection_ref: str = "",
) -> Dict[str, Any]:
    cfg = _load_feishu_config(user_id, ai_config_id, connection_ref)
    bot_cfg = read_feishu_config(cfg)
    text = normalize_feishu_text(text, strip_markdown=False)
    target_id, target_type = _resolve_feishu_target(bot_cfg, receive_id, receive_id_type)
    can_send_to_target = bool(target_id and bot_cfg.get("app_id") and bot_cfg.get("app_secret"))
    if bot_cfg.get("webhook_url") and not can_send_to_target:
        res = requests.post(
            bot_cfg.get("webhook_url", ""),
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"msg_type": "text", "content": {"text": str(text or "")}},
            timeout=20,
        )
        data = _feishu_json(res)
        code = data.get("code", data.get("StatusCode", 0)) if isinstance(data, dict) else 0
        if not res.ok or int(code or 0) != 0:
            raise HTTPException(status_code=502, detail=f"Feishu 仅通知发送失败: {data or res.text}")
        return {"success": True, "mode": "webhook", "raw": data}

    _validate_feishu_target(target_id, target_type)

    token = get_tenant_access_token(user_id, ai_config_id, connection_ref)
    res = requests.post(
        f"{FEISHU_OPEN_API_BASE}/im/v1/messages",
        params={"receive_id_type": target_type},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": target_id,
            "msg_type": "text",
            "content": json.dumps({"text": str(text or "")}, ensure_ascii=False),
        },
        timeout=20,
    )
    data = _feishu_json(res)
    _raise_feishu_error(res, data, "send_message")
    return _feishu_send_result(target_id, target_type, data)


def _send_feishu_open_message(
    cfg: AssistantAIConfig,
    *,
    token: str,
    receive_id: str,
    receive_id_type: str,
    msg_type: str,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    res = requests.post(
        f"{FEISHU_OPEN_API_BASE}/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        },
        timeout=20,
    )
    data = _feishu_json(res)
    _raise_feishu_error(res, data, f"send {msg_type}")
    return _feishu_send_result(receive_id, receive_id_type, data)


def upload_feishu_image(user_id: int, ai_config_id: Optional[int], source: MediaSource, connection_ref: str = "") -> str:
    _load_feishu_config(user_id, ai_config_id, connection_ref)
    token = get_tenant_access_token(user_id, ai_config_id, connection_ref)
    with open(source.path, "rb") as fh:
        res = requests.post(
            f"{FEISHU_OPEN_API_BASE}/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (source.filename, fh, source.mime_type)},
            timeout=60,
        )
    data = _feishu_json(res)
    _raise_feishu_error(res, data, "image upload")
    image_key = str((data.get("data") or {}).get("image_key") or "").strip()
    if not image_key:
        raise HTTPException(status_code=502, detail="Feishu image upload response missing image_key")
    return image_key


def upload_feishu_file(
    user_id: int,
    ai_config_id: Optional[int],
    source: MediaSource,
    *,
    file_type: str,
    duration: Optional[int] = None,
    connection_ref: str = "",
) -> str:
    _load_feishu_config(user_id, ai_config_id, connection_ref)
    token = get_tenant_access_token(user_id, ai_config_id, connection_ref)
    data = {"file_type": file_type, "file_name": source.filename}
    if duration is not None:
        data["duration"] = str(int(duration))
    with open(source.path, "rb") as fh:
        res = requests.post(
            f"{FEISHU_OPEN_API_BASE}/im/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files={"file": (source.filename, fh, source.mime_type or mimetypes.guess_type(source.filename)[0] or "application/octet-stream")},
            timeout=120,
        )
    parsed = _feishu_json(res)
    _raise_feishu_error(res, parsed, "file upload")
    file_key = str((parsed.get("data") or {}).get("file_key") or "").strip()
    if not file_key:
        raise HTTPException(status_code=502, detail="Feishu file upload response missing file_key")
    return file_key


def _feishu_file_type(source: MediaSource) -> str:
    suffix = os.path.splitext(source.filename or source.path)[1].lower()
    if suffix == ".opus":
        return "opus"
    if suffix in {".mp4", ".mov", ".m4v"}:
        return "mp4"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "doc"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "xls"
    if suffix in {".ppt", ".pptx"}:
        return "ppt"
    return "stream"


def send_feishu_media_message(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    media_url: str = "",
    media_path: str = "",
    media_type: str = "",
    file_name: str = "",
    receive_id: str = "",
    receive_id_type: str = "",
    **options,
) -> Dict[str, Any]:
    connection_ref = str(options.get("connection_ref") or "")
    duration = options.get("duration")
    cfg = _load_feishu_config(user_id, ai_config_id, connection_ref)
    bot_cfg = read_feishu_config(cfg)
    if not bot_cfg.get("app_id") or not bot_cfg.get("app_secret"):
        raise HTTPException(status_code=400, detail="Feishu media messages require App ID / App Secret")
    target_id, target_type = _resolve_feishu_target(bot_cfg, receive_id, receive_id_type)
    _validate_feishu_target(target_id, target_type)
    source = resolve_media_source(
        url=media_url,
        path=media_path,
        filename=file_name,
        max_bytes=30 * 1024 * 1024,
    )
    try:
        kind = infer_media_kind(source, media_type)
        token = get_tenant_access_token(user_id, ai_config_id, connection_ref)
        if kind == "image":
            image_key = upload_feishu_image(user_id, ai_config_id, source, connection_ref)
            return _send_feishu_open_message(
                cfg,
                token=token,
                receive_id=target_id,
                receive_id_type=target_type,
                msg_type="image",
                content={"image_key": image_key},
            )
        # 非图片统一走文件上传；视频使用 media 消息，其余使用 file 消息。
        file_key = upload_feishu_file(
            user_id,
            ai_config_id,
            source,
            file_type=_feishu_file_type(source),
            duration=duration,
            connection_ref=connection_ref,
        )
        if kind != "video":
            return _send_feishu_open_message(
                cfg,
                token=token,
                receive_id=target_id,
                receive_id_type=target_type,
                msg_type="file",
                content={"file_key": file_key},
            )
        return _send_feishu_open_message(
            cfg,
            token=token,
            receive_id=target_id,
            receive_id_type=target_type,
            msg_type="media",
            content={"file_key": file_key},
        )
    finally:
        source.cleanup()


def parse_feishu_text_event(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if not message:
        return None
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_type = str(sender.get("sender_type") or "").strip()
    if sender_type == "bot":
        return None
    message_type = str(message.get("message_type") or "").strip()
    if message_type and message_type != "text":
        return None
    content_raw = str(message.get("content") or "")
    text = ""
    try:
        content = json.loads(content_raw)
        text = str(content.get("text") or "").strip()
    except Exception:
        text = content_raw.strip()
    mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    for item in mentions:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key:
            text = text.replace(key, "")
    text = " ".join(text.split()).strip()
    if not text:
        return None
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    return {
        "text": text,
        "chat_id": str(message.get("chat_id") or "").strip(),
        "message_id": str(message.get("message_id") or "").strip(),
        "open_id": str(sender_id.get("open_id") or "").strip(),
        "user_id": str(sender_id.get("user_id") or "").strip(),
        "chat_type": str(message.get("chat_type") or "").strip(),
        "sender_type": sender_type,
    }
