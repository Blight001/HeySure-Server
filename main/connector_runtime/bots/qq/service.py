import json
import base64
import re
import threading
from typing import Any, Dict, Optional

import requests
from fastapi import HTTPException

from api.integrations.media_source import MediaSource, infer_media_kind, resolve_media_source
from api.models import AssistantAIConfig
from ..text_format import strip_markdown_to_plain
from ..transport import TokenCache, load_active_config, parse_json_response
from ._config import read_qq_config

QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
_TOKEN_CACHE = TokenCache()
_HTTP_LOCAL = threading.local()


def _qq_http_session() -> requests.Session:
    """Return a per-thread client that does not inherit desktop proxy vars."""
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _HTTP_LOCAL.session = session
    return session


def normalize_qq_text(text: str) -> str:
    return strip_markdown_to_plain(text, collapse_tables=False)


def _normalize_target_type(raw: str) -> str:
    value = str(raw or "c2c").strip().lower()
    aliases = {
        "user": "c2c",
        "private": "c2c",
        "openid": "c2c",
        "guild": "channel",
        "direct": "dm",
    }
    value = aliases.get(value, value)
    if value not in {"c2c", "group", "channel", "dm"}:
        raise HTTPException(status_code=400, detail=f"Unsupported QQ target_type: {value}")
    return value


def _validate_qq_credentials(bot_cfg: Dict[str, Any]) -> None:
    if not bot_cfg.get("app_id") or not bot_cfg.get("app_secret"):
        raise HTTPException(status_code=400, detail="QQ App ID / App Secret not configured")


def _load_qq_config(user_id: int, ai_config_id: Optional[int]) -> AssistantAIConfig:
    return load_active_config(
        user_id,
        ai_config_id,
        channel="qq",
        tool_name="qq",
        channel_label="QQ",
        read_config=read_qq_config,
        validate_credentials=_validate_qq_credentials,
    )


def get_qq_access_token(user_id: int, ai_config_id: Optional[int]) -> str:
    cfg = _load_qq_config(user_id, ai_config_id)
    bot_cfg = read_qq_config(cfg)

    def _fetch() -> "tuple[str, int]":
        res = _qq_http_session().post(
            QQ_TOKEN_URL,
            headers={"Content-Type": "application/json"},
            json={"appId": str(bot_cfg.get("app_id") or ""), "clientSecret": str(bot_cfg.get("app_secret") or "")},
            timeout=20,
        )
        data = parse_json_response(res)
        if not res.ok or not data.get("access_token"):
            raise HTTPException(status_code=502, detail=f"QQ token failed: {data or res.text}")
        return str(data.get("access_token") or "").strip(), int(data.get("expires_in") or 7200)

    return _TOKEN_CACHE.get_or_fetch(int(cfg.id or 0), _fetch)


def _qq_api_base(cfg: AssistantAIConfig) -> str:
    return QQ_SANDBOX_API_BASE if bool(read_qq_config(cfg).get("sandbox")) else QQ_API_BASE


def _message_endpoint(cfg: AssistantAIConfig, target_type: str, target_id: str) -> str:
    base = _qq_api_base(cfg)
    if target_type == "c2c":
        return f"{base}/v2/users/{target_id}/messages"
    if target_type == "group":
        return f"{base}/v2/groups/{target_id}/messages"
    if target_type == "channel":
        return f"{base}/channels/{target_id}/messages"
    if target_type == "dm":
        return f"{base}/dms/{target_id}/messages"
    raise HTTPException(status_code=400, detail=f"Unsupported QQ target_type: {target_type}")


def _qq_headers(cfg: AssistantAIConfig, token: str) -> Dict[str, str]:
    return {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
        "X-Union-Appid": str(read_qq_config(cfg).get("app_id") or ""),
    }


def _post_qq_message(cfg: AssistantAIConfig, *, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = get_qq_access_token(cfg.user_id, cfg.id)
    res = _qq_http_session().post(
        endpoint,
        headers=_qq_headers(cfg, token),
        json=payload,
        timeout=20,
    )
    data = parse_json_response(res)
    if not res.ok or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        raise HTTPException(status_code=502, detail=f"QQ send_message failed: {data or res.text}")
    return data


# --------------------------------------------------------------------------- #
# Shared send-path helpers — the target resolution, reply-context injection,
# passive-id retry and result shaping that every QQ send function repeated.
# --------------------------------------------------------------------------- #

def _resolve_qq_target(bot_cfg: Dict[str, Any], target_id: str, target_type: str) -> tuple[str, str]:
    """Apply the default-receiver fallback + type normalization, once."""
    final_target_id = str(target_id or bot_cfg.get("default_target_id") or "").strip()
    final_target_type = _normalize_target_type(target_type or bot_cfg.get("default_target_type") or "c2c")
    if not final_target_id:
        raise HTTPException(status_code=400, detail="QQ target_id is required")
    return final_target_id, final_target_type


def _apply_reply_context(
    payload: Dict[str, Any], *, msg_id: str, event_id: str, msg_seq: Optional[int]
) -> None:
    """Attach the passive reply ids (msg_id/msg_seq or event_id) onto ``payload``."""
    if msg_id:
        payload["msg_id"] = str(msg_id)
        if msg_seq is not None:
            payload["msg_seq"] = int(msg_seq)
    if event_id and not msg_id:
        payload["event_id"] = str(event_id)


def _strip_passive_ids(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``payload`` with the passive reply ids removed."""
    out = dict(payload)
    out.pop("msg_id", None)
    out.pop("msg_seq", None)
    out.pop("event_id", None)
    return out


def _post_with_passive_retry(
    cfg: AssistantAIConfig, *, endpoint: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """POST a message, retrying once as an active send if a passive id is stale.

    Mirrors the shared behavior of the text/media senders: a stale ``msg_id``
    is the most common rejection, so drop the passive ids and resend.
    """
    try:
        return _post_qq_message(cfg, endpoint=endpoint, payload=payload)
    except HTTPException:
        if "msg_id" not in payload:
            raise
        return _post_qq_message(cfg, endpoint=endpoint, payload=_strip_passive_ids(payload))


def _qq_send_result(target_id: str, target_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "target_id": target_id,
        "target_type": target_type,
        "message_id": data.get("id") if isinstance(data, dict) else None,
        "raw": data,
    }


def _qq_media_file_endpoint(cfg: AssistantAIConfig, target_type: str, target_id: str) -> str:
    base = _qq_api_base(cfg)
    if target_type == "c2c":
        return f"{base}/v2/users/{target_id}/files"
    if target_type == "group":
        return f"{base}/v2/groups/{target_id}/files"
    raise HTTPException(status_code=400, detail="QQ media upload currently supports c2c or group targets")


def upload_qq_media_file_info(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    source: MediaSource,
    target_id: str,
    target_type: str,
    media_type: str,
) -> str:
    cfg = _load_qq_config(user_id, ai_config_id)
    final_target_type = _normalize_target_type(target_type)
    if final_target_type not in {"c2c", "group"}:
        raise HTTPException(status_code=400, detail="QQ image/video messages are supported for c2c or group targets")
    kind = infer_media_kind(source, media_type)
    file_type = 1 if kind == "image" else 2
    payload: Dict[str, Any] = {
        "file_type": file_type,
        "srv_send_msg": False,
    }
    if source.source_url:
        payload["url"] = source.source_url
    else:
        with open(source.path, "rb") as fh:
            payload["file_data"] = base64.b64encode(fh.read()).decode("ascii")
    token = get_qq_access_token(user_id, ai_config_id)
    res = _qq_http_session().post(
        _qq_media_file_endpoint(cfg, final_target_type, target_id),
        headers=_qq_headers(cfg, token),
        json=payload,
        timeout=60,
    )
    data = parse_json_response(res)
    if not res.ok or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        raise HTTPException(status_code=502, detail=f"QQ media upload failed: {data or res.text}")
    file_info = str(data.get("file_info") or (data.get("data") or {}).get("file_info") or "").strip()
    if not file_info:
        raise HTTPException(status_code=502, detail="QQ media upload response missing file_info")
    return file_info


def send_qq_text_message(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    text: str,
    target_id: str = "",
    target_type: str = "",
    msg_id: str = "",
    event_id: str = "",
    msg_seq: Optional[int] = None,
) -> Dict[str, Any]:
    cfg = _load_qq_config(user_id, ai_config_id)
    bot_cfg = read_qq_config(cfg)
    body = normalize_qq_text(text)
    if not body:
        raise HTTPException(status_code=400, detail="QQ message text is required")
    final_target_id, final_target_type = _resolve_qq_target(bot_cfg, target_id, target_type)

    payload: Dict[str, Any] = {"content": body, "msg_type": 0}
    _apply_reply_context(payload, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq)

    endpoint = _message_endpoint(cfg, final_target_type, final_target_id)
    data = _post_with_passive_retry(cfg, endpoint=endpoint, payload=payload)
    return _qq_send_result(final_target_id, final_target_type, data)


def _prepare_markdown_text(text: str) -> str:
    """Light cleanup that *keeps* markdown syntax (unlike ``normalize_qq_text``).

    QQ native markdown renders the body as-is, so we only normalize line
    endings and collapse runs of blank lines — headings, lists, links, code
    fences, emphasis, etc. are preserved.
    """
    body = str(text or "")
    if not body:
        return ""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _prepare_stream_markdown_text(text: str) -> str:
    """Normalize line endings without stripping incremental whitespace."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _qq_markdown_field(content: str, markdown_mode: str, template_id: str) -> Dict[str, Any]:
    """Build the ``markdown`` object for a ``msg_type=2`` message.

    Two mutually exclusive shapes (per QQ open-platform spec):
      - native:   ``{"content": "<raw markdown>"}``
      - template: ``{"custom_template_id": "<id>", "params": [{key, values}]}``

    Template mode assumes the approved template exposes a single ``content``
    placeholder; bots with multi-field templates should send via the explicit
    MCP tool instead.
    """
    mode = str(markdown_mode or "native").strip().lower()
    tpl = str(template_id or "").strip()
    if mode == "template" and tpl:
        return {
            "custom_template_id": tpl,
            "params": [{"key": "content", "values": [content]}],
        }
    return {"content": content}


def send_qq_markdown_message(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    text: str,
    target_id: str = "",
    target_type: str = "",
    msg_id: str = "",
    event_id: str = "",
    msg_seq: Optional[int] = None,
    markdown_mode: str = "native",
    template_id: str = "",
    fallback_plain: bool = True,
) -> Dict[str, Any]:
    """Send a ``msg_type=2`` markdown reply, auto-falling back to plain text.

    Markdown is a whitelist feature; when the gateway rejects the markdown
    payload (and ``fallback_plain`` is set) we transparently resend the same
    body as a normal ``msg_type=0`` text message so delivery never silently
    fails.
    """
    cfg = _load_qq_config(user_id, ai_config_id)
    bot_cfg = read_qq_config(cfg)
    content = _prepare_markdown_text(text)
    if not content:
        raise HTTPException(status_code=400, detail="QQ message text is required")
    final_target_id, final_target_type = _resolve_qq_target(bot_cfg, target_id, target_type)

    payload: Dict[str, Any] = {
        "msg_type": 2,
        "markdown": _qq_markdown_field(content, markdown_mode, template_id),
    }
    _apply_reply_context(payload, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq)

    endpoint = _message_endpoint(cfg, final_target_type, final_target_id)
    try:
        data = _post_qq_message(cfg, endpoint=endpoint, payload=payload)
    except HTTPException:
        # Stale passive ids are the most common rejection; retry as an
        # active markdown message before giving up on markdown entirely.
        if "msg_id" in payload or "event_id" in payload:
            try:
                data = _post_qq_message(cfg, endpoint=endpoint, payload=_strip_passive_ids(payload))
                return _qq_send_result(final_target_id, final_target_type, data)
            except HTTPException:
                pass
        if not fallback_plain:
            raise
        # Markdown not available for this bot — degrade to plain text.
        return send_qq_text_message(
            user_id,
            ai_config_id,
            text=text,
            target_id=final_target_id,
            target_type=final_target_type,
            msg_id=msg_id,
            event_id=event_id,
            msg_seq=msg_seq,
        )
    return _qq_send_result(final_target_id, final_target_type, data)


def post_qq_stream_packet(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    text: str,
    target_id: str,
    target_type: str,
    stream_id: str,
    stream_index: int,
    stream_state: int,
    reset: bool = False,
    msg_id: str = "",
    event_id: str = "",
    msg_seq: Optional[int] = None,
    markdown_mode: str = "native",
    template_id: str = "",
) -> Dict[str, Any]:
    """POST one packet of a streaming markdown message.

    NOTE — streaming is a QQ 灰度 capability and the ``stream`` object shape is
    documented only on the (access-restricted) official ``send.html`` page.
    The fields below match the published spec; verify the ``state`` enum
    against your console before relying on it:

        stream = {
            "state": 1,   # 1 = 生成中 (first/intermediate packet), 10 = 完成 (final)
            "id":    "",  # omitted on the first packet, then QQ's returned id
            "index": 0,   # first packet is 0, then strictly increases
            "reset": False,  # True replaces the whole message body
        }

    Returns the raw response so the caller can capture a server-assigned
    stream ``id`` from the first packet. Raises ``HTTPException`` on rejection
    so the caller can fall back to a normal send.
    """
    cfg = _load_qq_config(user_id, ai_config_id)
    final_target_type = _normalize_target_type(target_type or "c2c")
    content = _prepare_stream_markdown_text(text)
    if stream_state == 10 and content and not content.endswith("\n"):
        content += "\n"
    if not content:
        raise HTTPException(status_code=400, detail="QQ stream text is required")
    stream_payload: Dict[str, Any] = {
        "state": int(stream_state),
        "index": int(stream_index),
        "reset": bool(reset),
    }
    # The first packet has no stream id. QQ returns the id that subsequent
    # packets must reuse.
    if stream_id:
        stream_payload["id"] = str(stream_id)
    payload: Dict[str, Any] = {
        "msg_type": 2,
        "markdown": _qq_markdown_field(content, markdown_mode, template_id),
        "stream": stream_payload,
    }
    _apply_reply_context(payload, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq)
    endpoint = _message_endpoint(cfg, final_target_type, str(target_id or "").strip())
    return _post_qq_message(cfg, endpoint=endpoint, payload=payload)


def diagnose_qq_config(user_id: int, ai_config_id: Optional[int]) -> Dict[str, Any]:
    cfg = _load_qq_config(user_id, ai_config_id)
    bot_cfg = read_qq_config(cfg)
    token = get_qq_access_token(user_id, ai_config_id)
    return {
        "success": True,
        "ai_config_id": int(cfg.id or 0),
        "bot_channel": str(cfg.bot_channel or ""),
        "qq_enabled": bool(bot_cfg.get("enabled")),
        "app_id_configured": bool(str(bot_cfg.get("app_id") or "").strip()),
        "app_secret_configured": bool(str(bot_cfg.get("app_secret") or "").strip()),
        "sandbox": bool(bot_cfg.get("sandbox")),
        "api_base": _qq_api_base(cfg),
        "default_target_id_configured": bool(str(bot_cfg.get("default_target_id") or "").strip()),
        "default_target_type": str(bot_cfg.get("default_target_type") or "c2c"),
        "token_ok": bool(token),
        "token_preview": f"{token[:6]}..." if token else "",
    }


def send_qq_media_message(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    media_url: str = "",
    media_path: str = "",
    media_type: str = "",
    file_name: str = "",
    target_id: str = "",
    target_type: str = "",
    text: str = "",
    msg_id: str = "",
    event_id: str = "",
    msg_seq: Optional[int] = None,
) -> Dict[str, Any]:
    cfg = _load_qq_config(user_id, ai_config_id)
    bot_cfg = read_qq_config(cfg)
    final_target_id, final_target_type = _resolve_qq_target(bot_cfg, target_id, target_type)
    if final_target_type not in {"c2c", "group"}:
        raise HTTPException(status_code=400, detail="QQ image/video messages are supported for c2c or group targets")
    source = resolve_media_source(
        url=media_url,
        path=media_path,
        filename=file_name,
        max_bytes=30 * 1024 * 1024,
    )
    try:
        file_info = upload_qq_media_file_info(
            user_id,
            ai_config_id,
            source=source,
            target_id=final_target_id,
            target_type=final_target_type,
            media_type=media_type,
        )
    finally:
        source.cleanup()

    payload: Dict[str, Any] = {
        "msg_type": 7,
        "media": {"file_info": file_info},
    }
    content = normalize_qq_text(text)
    if content:
        payload["content"] = content
    _apply_reply_context(payload, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq)

    endpoint = _message_endpoint(cfg, final_target_type, final_target_id)
    data = _post_with_passive_retry(cfg, endpoint=endpoint, payload=payload)
    return _qq_send_result(final_target_id, final_target_type, data)


def parse_qq_text_event(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    event_type = str(payload.get("t") or "").strip()
    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    if not data:
        return None
    if event_type not in {
        "C2C_MESSAGE_CREATE",
        "GROUP_AT_MESSAGE_CREATE",
        "AT_MESSAGE_CREATE",
        "MESSAGE_CREATE",
        "DIRECT_MESSAGE_CREATE",
    }:
        return None

    text = str(data.get("content") or "").strip()
    if not text:
        return None
    # QQ group/channel events may include the bot mention in content.
    text = re.sub(r"<@!?\d+>", "", text).strip()
    text = " ".join(text.split()).strip()
    if not text:
        return None

    message_id = str(data.get("id") or "").strip()
    event_id = str(payload.get("id") or data.get("event_id") or "").strip()
    if event_type == "C2C_MESSAGE_CREATE":
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        target_id = str(data.get("author_id") or author.get("user_openid") or author.get("id") or data.get("user_openid") or "").strip()
        target_type = "c2c"
    elif event_type == "GROUP_AT_MESSAGE_CREATE":
        target_id = str(data.get("group_openid") or data.get("group_id") or "").strip()
        target_type = "group"
    elif event_type == "DIRECT_MESSAGE_CREATE":
        target_id = str(data.get("guild_id") or "").strip()
        target_type = "dm"
    else:
        target_id = str(data.get("channel_id") or "").strip()
        target_type = "channel"

    if not target_id:
        return None
    return {
        "text": text,
        "message_id": message_id,
        "event_id": event_id,
        "target_id": target_id,
        "target_type": target_type,
        "event_type": event_type,
        "raw": json.dumps(data, ensure_ascii=False),
    }
