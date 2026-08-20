"""通信类 MCP 工具：

- message.send+to → 统一发消息工具，按 ``to`` 参数分发收件方：
    - to="user"（或省略且不带 AI 寻址参数）→ 向用户发送消息
      （按 AI 配置选择对应机器人插件）；
    - to=成员 ID 或名字（或带 to_ai_config_id / to_ai_name）→ 向另一个 AI
      发送消息。所有"回信"都走它本身：带 message_type="reply" 与
      reply_to_message_id。系统按 (target_session_id, status) 严格匹配。

历史上是 message.send+to+user / message.send+to+ai 两个工具，2026-07 合并；
旧名经 mcp_tool_aliases.LEGACY_TOOL_RENAMES 归一到本工具。
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from connector_runtime.bots.messaging import MediaPayload, Recipient, dispatcher
from api.database import engine
from mcp_runtime.mcp.core import get_project_root
from api.models import AssistantAIConfig, BotConnection, BotContact, BotSessionRoute
from api.services.bot_directory import resolve_contact_target
from api.services.storage.workspace_files import register_workspace_file, resolve_file_ref
from api.services.storage.temporary_file_links import (
    configured_public_base_url,
    create_temporary_file_link,
)
from ai_runtime.inference import ai_message_service
from api.runtime.run_context import get_run_session_context


_ALLOWED_MESSAGE_TYPES = {"inquiry", "reply", "notify"}
_MESSAGE_TYPE_HINT = (
    'message_type is required. Use "inquiry" for a question/request that expects an answer, '
    '"reply" for answering a previous inquiry, or "notify" for a one-way notification/status/result '
    'that does not expect an answer.'
)
DEFAULT_REPLY_WAIT_SECONDS = 24 * 60 * 60


def _resolve_server_media_path(user_id: int, ai_config_id: Optional[int], media_path: str) -> str:
    value = str(media_path or "").strip()
    if not value:
        return ""
    root = get_project_root(user_id, ai_config_id)
    candidate = value if os.path.isabs(value) else os.path.join(root, value.replace("\\", "/"))
    root_real = os.path.realpath(root)
    candidate_real = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([root_real, candidate_real])
    except ValueError:
        common = ""
    if common != root_real:
        raise HTTPException(
            status_code=403,
            detail={"code": "FILE_SCOPE_VIOLATION", "message": "media path must stay inside the current AI workspace"},
        )
    return candidate_real


def _legacy_media_payload(
    user_id: int, ai_config_id: Optional[int], args: Dict[str, Any], *, channel: str = ""
) -> Optional[MediaPayload]:
    media_url = str(
        args.get("media_url") or args.get("image_url") or args.get("video_url") or args.get("file_url") or ""
    ).strip()
    media_path = str(
        args.get("media_path") or args.get("image_path") or args.get("video_path") or args.get("file_path") or ""
    ).strip()
    file_ref = str(args.get("file_ref") or "").strip()
    file_name = str(args.get("file_name") or args.get("filename") or "").strip()
    media_type = str(
        args.get("media_type")
        or ("image" if (args.get("image_url") or args.get("image_path")) else "")
        or ("video" if (args.get("video_url") or args.get("video_path")) else "")
    ).strip()
    if file_ref:
        resolved = resolve_file_ref(user_id=user_id, ai_config_id=ai_config_id, file_ref=file_ref)
        media_path = str(resolved["server_path"])
        file_name = file_name or str(resolved["file_name"])
    elif media_path:
        media_path = _resolve_server_media_path(user_id, ai_config_id, media_path)
    # QQ's file_data JSON upload becomes unreliable once base64 expands a
    # multi-megabyte video. Give transports a short-lived public capability
    # URL while retaining the local path as a fallback. The URL contains no
    # workspace path and expires automatically after 15 minutes.
    if channel == "qq" and media_path and not media_url and ai_config_id:
        if not file_ref:
            root = get_project_root(user_id, ai_config_id)
            registered = register_workspace_file(
                user_id=user_id,
                ai_config_id=ai_config_id,
                workspace_path=os.path.relpath(media_path, root).replace(os.sep, "/"),
                file_name=file_name or os.path.basename(media_path),
            )
            file_ref = str(registered["file_ref"])
        link = create_temporary_file_link(
            user_id=user_id,
            ai_config_id=int(ai_config_id),
            file_ref=file_ref,
            public_base_url=configured_public_base_url(),
            ttl_seconds=900,
        )
        media_url = str(link["url"])
    if not media_url and not media_path:
        return None
    return MediaPayload(
        url=media_url,
        path=media_path,
        media_type=media_type,
        file_name=file_name,
        duration=args.get("duration"),
    )


def _attachment_payloads(
    user_id: int, ai_config_id: Optional[int], args: Dict[str, Any], *, channel: str = ""
) -> List[MediaPayload]:
    raw = args.get("attachments")
    if raw is None:
        legacy = _legacy_media_payload(user_id, ai_config_id, args, channel=channel)
        return [legacy] if legacy else []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="attachments must be a non-empty array")
    if len(raw) > 5:
        raise HTTPException(status_code=400, detail="at most 5 attachments may be sent at once")
    payloads: List[MediaPayload] = []
    for item in raw:
        values = {"file_ref": item} if isinstance(item, str) else item
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="each attachment must be a file_ref string or object")
        payload = _legacy_media_payload(user_id, ai_config_id, values, channel=channel)
        if payload is None:
            raise HTTPException(status_code=400, detail="each attachment requires file_ref, media_url, or media_path")
        payloads.append(payload)
    return payloads


def _send_attachment_batch(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    channel: str,
    recipient: Optional[Recipient],
    raw_target: Dict[str, Any],
    text: str,
    attachments: List[MediaPayload],
) -> tuple[List[Any], Optional[Exception]]:
    deliveries = []
    for index, media in enumerate(attachments):
        try:
            deliveries.append(dispatcher.send_media(
                user_id=user_id,
                ai_config_id=ai_config_id,
                channel=channel,
                media=MediaPayload(
                    text=text if index == 0 else "",
                    url=media.url,
                    path=media.path,
                    media_type=media.media_type,
                    file_name=media.file_name,
                    duration=media.duration,
                ),
                recipient=recipient,
                raw_target=None if recipient is not None else raw_target,
            ))
        except Exception as exc:
            return deliveries, exc
    return deliveries, None


def _coerce_message_type(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in _ALLOWED_MESSAGE_TYPES:
        return text
    raise HTTPException(status_code=400, detail=_MESSAGE_TYPE_HINT)


# ---------- 与用户通信 ----------

def _qq_not_bound(message: str, *, reason: str) -> Dict[str, Any]:
    """Return a model-readable binding failure instead of a transport error."""
    return {
        "delivered": False,
        "channel": "qq",
        "reason": reason,
        "message": message,
    }


def _resolve_qq_notification_recipient(
    user_id: int,
    ai_config_id: Optional[int],
) -> tuple[Optional[Recipient], str, Optional[Dict[str, Any]]]:
    """Resolve the QQ user currently bound to this AI without caller ids.

    Priority is deliberately deterministic:
    1. the QQ route for the MCP call's current conversation;
    2. the default target explicitly configured for this AI;
    3. the most recently used QQ route for web/background conversations.
    """
    if not ai_config_id:
        return None, "", _qq_not_bound(
            "当前 AI 未绑定 QQ 连接（缺少 AI 配置上下文）。",
            reason="qq_ai_config_missing",
        )

    run_ctx = get_run_session_context() or {}
    current_session_id = str(run_ctx.get("session_id") or "").strip()
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == int(ai_config_id),
                AssistantAIConfig.user_id == int(user_id),
            )
        ).first()
        if cfg is None:
            return None, "", _qq_not_bound(
                "当前 AI 未绑定 QQ 连接（未找到对应的 AI 配置）。",
                reason="qq_ai_config_not_found",
            )
        from connector_runtime.bots.qq._config import read_qq_config
        from connector_runtime.bots.qq.routes_store import find_qq_bound_target

        qq_cfg = read_qq_config(cfg)
        if not qq_cfg.get("enabled"):
            return None, "", _qq_not_bound(
                "当前 AI 未绑定可用的 QQ 连接（QQ 机器人未启用）。",
                reason="qq_not_enabled",
            )
        if not str(qq_cfg.get("app_id") or "").strip() or not str(qq_cfg.get("app_secret") or "").strip():
            return None, "", _qq_not_bound(
                "当前 AI 未绑定可用的 QQ 连接（App ID / App Secret 配置不完整）。",
                reason="qq_credentials_missing",
            )

        ai_kind = str(run_ctx.get("ai_kind") or "").strip()
        if ai_kind not in {"assistant", "core"}:
            ai_kind = "assistant" if cfg.ai_role == "assistant_admin" else "core"

        if current_session_id:
            current = find_qq_bound_target(
                session,
                user_id=user_id,
                ai_config_id=int(ai_config_id),
                ai_kind=ai_kind,
                session_id=current_session_id,
            )
            if current is not None:
                return Recipient(to_id=current.target_id, to_type=current.target_type), "current_qq_session", None

        default_target_id = str(qq_cfg.get("default_target_id") or "").strip()
        if default_target_id:
            default_target_type = str(qq_cfg.get("default_target_type") or "c2c").strip() or "c2c"
            return Recipient(to_id=default_target_id, to_type=default_target_type), "configured_default", None

        recent = find_qq_bound_target(
            session,
            user_id=user_id,
            ai_config_id=int(ai_config_id),
            ai_kind=ai_kind,
        )
        if recent is not None:
            return Recipient(to_id=recent.target_id, to_type=recent.target_type), "recent_qq_binding", None

    return None, "", _qq_not_bound(
        "当前 AI 尚未绑定 QQ 接收用户或会话；请先让用户通过 QQ 给该 AI 发送一条消息，或配置默认接收目标。",
        reason="qq_recipient_not_bound",
    )


def _notification_attachment_records(
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
) -> List[Dict[str, Any]]:
    raw = args.get("attachments")
    if raw is None:
        file_ref = str(args.get("file_ref") or "").strip()
        raw = [{"file_ref": file_ref}] if file_ref else []
    records: List[Dict[str, Any]] = []
    for value in raw if isinstance(raw, list) else []:
        item = {"file_ref": value} if isinstance(value, str) else value
        if not isinstance(item, dict):
            continue
        file_ref = str(item.get("file_ref") or "").strip()
        if file_ref:
            resolved = resolve_file_ref(user_id=user_id, ai_config_id=ai_config_id, file_ref=file_ref)
            records.append({
                "file_ref": file_ref,
                "file_name": resolved.get("file_name") or "file",
                "mime_type": resolved.get("mime_type") or "application/octet-stream",
                "bytes": resolved.get("bytes") or 0,
            })
        else:
            records.append({
                "file_name": item.get("file_name") or item.get("filename") or "file",
                "mime_type": item.get("media_type") or "application/octet-stream",
            })
    return records


def _external_recipient(
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
    channel: str,
) -> tuple[Optional[Recipient], str, Optional[Dict[str, Any]]]:
    if channel != "qq":
        return None, "", None
    qq_bot = dispatcher.resolve_bot("qq")
    explicit = qq_bot.parse_recipient(args) if qq_bot is not None else Recipient()
    if explicit.is_explicit:
        return explicit, "explicit", None
    return _resolve_qq_notification_recipient(user_id, ai_config_id)


def _scoped_external_recipient(
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
) -> tuple[Optional[str], Optional[Recipient], str, bool]:
    """Resolve opaque refs/current bot route without exposing provider ids."""
    if not ai_config_id:
        return None, None, "", False
    connection_ref = str(args.get("connection_ref") or "").strip()
    contact_ref = str(args.get("recipient_ref") or "").strip()
    if bool(connection_ref) != bool(contact_ref):
        raise HTTPException(status_code=400, detail={
            "code": "BOT_TARGET_INCOMPLETE",
            "message": "connection_ref and recipient_ref must be provided together",
        })
    run_ctx = get_run_session_context() or {}
    current_session_id = str(run_ctx.get("session_id") or "").strip()
    if not contact_ref and not current_session_id:
        return None, None, "", False
    with Session(engine) as session:
        current_route = None
        if current_session_id:
            current_route = session.exec(select(BotSessionRoute).where(
                BotSessionRoute.user_id == int(user_id),
                BotSessionRoute.ai_config_id == int(ai_config_id),
                BotSessionRoute.session_id == current_session_id,
            )).first()
        current_contact = session.get(BotContact, current_route.contact_id) if current_route and current_route.contact_id else None
        current_connection = session.get(BotConnection, current_route.connection_id) if current_route and current_route.connection_id else None
        if contact_ref:
            if current_contact is not None and current_contact.contact_ref != contact_ref:
                raise HTTPException(status_code=403, detail={
                    "code": "BOT_CONTACT_SCOPE_VIOLATION",
                    "message": "bot-originated runs may only message their current contact",
                })
            resolved = resolve_contact_target(
                session,
                user_id=user_id,
                ai_config_id=int(ai_config_id),
                connection_ref=connection_ref,
                contact_ref=contact_ref,
            )
            if resolved is None:
                raise HTTPException(status_code=404, detail={"code": "BOT_TARGET_NOT_FOUND", "message": "bot target not found"})
            bot = dispatcher.resolve_bot(resolved.connection.channel)
            if bot is None:
                raise HTTPException(status_code=400, detail="bot channel is not supported")
            target = {**resolved.target, "connection_ref": resolved.connection.connection_ref}
            return resolved.connection.channel, bot.parse_recipient(target), "explicit_ref", True
        if current_contact is not None and current_connection is not None:
            resolved = resolve_contact_target(
                session,
                user_id=user_id,
                ai_config_id=int(ai_config_id),
                connection_ref=current_connection.connection_ref,
                contact_ref=current_contact.contact_ref,
            )
            if resolved is not None:
                bot = dispatcher.resolve_bot(resolved.connection.channel)
                if bot is not None:
                    target = {**resolved.target, "connection_ref": resolved.connection.connection_ref}
                    return resolved.connection.channel, bot.parse_recipient(target), "current_contact", True
    return None, None, "", False


def _ensure_unambiguous_owner_channel(user_id: int, ai_config_id: Optional[int], explicit_channel: str) -> None:
    if explicit_channel or not ai_config_id:
        return
    from connector_runtime.bots.registry import iter_active_for_config
    try:
        with Session(engine) as session:
            cfg = session.exec(select(AssistantAIConfig).where(
                AssistantAIConfig.id == int(ai_config_id),
                AssistantAIConfig.user_id == int(user_id),
            )).first()
            enabled = [bot.channel for bot in iter_active_for_config(cfg)] if cfg else []
            contacts = session.exec(select(BotContact, BotConnection).join(
                BotConnection, BotConnection.id == BotContact.connection_id
            ).where(
                BotContact.user_id == int(user_id),
                BotContact.ai_config_id == int(ai_config_id),
                BotContact.enabled.is_(True),
                BotConnection.enabled.is_(True),
            ).order_by(BotContact.last_seen_at.desc())).all()
    except Exception:
        return
    if len(enabled) > 1 or len(contacts) > 1:
        targets = [
            {
                "channel": connection.channel,
                "connection_ref": connection.connection_ref,
                "recipient_ref": contact.contact_ref,
                "display_name": contact.display_name or "未命名联系人",
            }
            for contact, connection in contacts[:20]
        ]
        raise HTTPException(status_code=409, detail={
            "code": "BOT_TARGET_AMBIGUOUS",
            "message": "multiple bot targets are available; provide connection_ref and recipient_ref",
            "channels": enabled,
            "targets": targets,
        })


def _send_external_user_message(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    args: Dict[str, Any],
    text: str,
    attachments: List[MediaPayload],
    channel: str,
    recipient: Optional[Recipient],
) -> tuple[List[Any], Optional[Exception]]:
    if attachments:
        return _send_attachment_batch(
            user_id=user_id,
            ai_config_id=ai_config_id,
            channel=channel,
            recipient=recipient,
            raw_target=args,
            text=text,
            attachments=attachments,
        )
    try:
        return [dispatcher.send_text(
            user_id=user_id,
            ai_config_id=ai_config_id,
            channel=channel,
            text=text,
            recipient=recipient,
            raw_target=None if recipient is not None else args,
        )], None
    except Exception as exc:
        return [], exc


def _delivery_message_ids(deliveries: List[Any]) -> List[str]:
    sent_ids: List[str] = []
    for item in deliveries:
        result = item.detail
        if not isinstance(result, dict):
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        sent_id = result.get("message_id") or result.get("msg_id") or data.get("message_id")
        if sent_id:
            sent_ids.append(str(sent_id))
    return sent_ids


def _persist_user_notification(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    text: str,
    attachment_records: List[Dict[str, Any]],
    channel: str,
    external_delivered: bool,
) -> str:
    from api.services.notifications.user_notifications import create_notification

    with Session(engine) as session:
        item = create_notification(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            body=text or f"发送了 {len(attachment_records)} 个文件",
            attachments=attachment_records,
            app_push_required=not external_delivered,
            external_channel=channel,
            external_delivered=external_delivered,
        )
    return item.id


def _user_send_message(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """Send through a bound bot, with the durable HeySure inbox as fallback."""
    text = str(args.get("text") or args.get("content") or args.get("message") or "").strip()
    attachment_records = _notification_attachment_records(user_id, ai_config_id, args)
    requested_channel = str(args.get("channel") or "").strip().lower()
    scoped_channel, scoped_recipient, binding_source, target_scoped = _scoped_external_recipient(
        user_id, ai_config_id, args
    )
    if scoped_channel:
        channel, recipient, unavailable = scoped_channel, scoped_recipient, None
    else:
        _ensure_unambiguous_owner_channel(user_id, ai_config_id, requested_channel)
        channel = dispatcher.resolve_channel(requested_channel or None, ai_config_id, user_id)
        recipient, binding_source, unavailable = _external_recipient(user_id, ai_config_id, args, channel)
    attachments = _attachment_payloads(user_id, ai_config_id, args, channel=channel)
    if not text and not attachments:
        raise HTTPException(status_code=400, detail="text or an attachment is required when message.send+to targets the user")
    deliveries: List[Any] = []
    external_error: Optional[Exception] = None
    if unavailable is None:
        deliveries, external_error = _send_external_user_message(
            user_id=user_id, ai_config_id=ai_config_id, args=args, text=text,
            attachments=attachments, channel=channel, recipient=recipient,
        )
    external_delivered = bool(deliveries) and all(bool(item.ok) for item in deliveries) and external_error is None
    if target_scoped and not external_delivered:
        return {
            "accepted": False,
            "delivered": False,
            "pending": False,
            "fallback_used": False,
            "channel": channel,
            "binding_source": binding_source,
            "error": type(external_error).__name__ if external_error else "external_delivery_failed",
        }
    notification_id = _persist_user_notification(
        user_id=user_id,
        ai_config_id=ai_config_id,
        text=text,
        attachment_records=attachment_records,
        channel=channel,
        external_delivered=external_delivered,
    )
    out: Dict[str, Any] = {
        "accepted": True,
        "delivered": external_delivered,
        "pending": not external_delivered,
        "fallback_used": not external_delivered,
        "channel": channel if external_delivered else "heysure",
        "external_channel": channel,
        "delivery_status": "external_delivered" if external_delivered else "app_pending",
        "notification_id": notification_id,
    }
    if attachment_records:
        out["attachment_count"] = len(attachment_records)
    if binding_source:
        out["binding_source"] = binding_source
    if unavailable is not None:
        out["fallback_reason"] = unavailable.get("reason") or "bot_not_bound"
    elif external_error is not None:
        out["fallback_reason"] = type(external_error).__name__
        if deliveries:
            out["partial"] = True
            out["sent_count"] = len(deliveries)
    sent_ids = _delivery_message_ids(deliveries)
    if sent_ids:
        out["message_id"] = sent_ids[0]
        if len(sent_ids) > 1:
            out["message_ids"] = sent_ids
    return out


# ---------- AI 间通信 ----------


def _emit_ai_message_event(user_id: int, from_id: int, to_id: int, kind: str) -> None:
    """世界页信使演出通知。best-effort，失败不影响消息投递。"""
    try:
        from api.services.world_events import emit_world_event

        emit_world_event(user_id, "ai_message", {
            "from_ai_config_id": from_id,
            "to_ai_config_id": to_id,
            "kind": kind,
        })
    except Exception:
        pass


def _reply_result(
    *,
    user_id: int,
    completed_reply: Dict[str, Any],
    ai_config_id: int,
    to_id: int,
    return_session_id: str,
) -> Dict[str, Any]:
    _emit_ai_message_event(user_id, ai_config_id, to_id, "reply")
    return {
        "message_id": completed_reply.get("message_id"),
        "replied": True,
        "status": "replied",
        "to_ai_config_id": to_id,
        "reply_to_message_id": completed_reply.get("reply_to_message_id") or completed_reply.get("message_id"),
        "note": "已作为上一封 AI 消息的回信送达原会话。",
    }


def _resolve_target_ai_id_by_name(user_id: int, name: str) -> int:
    """按名字查目标 AI 的 ai_config_id；找不到/重名时报带候选列表的错误。"""
    from sqlmodel import select

    from api.models import AssistantAIConfig

    wanted = str(name or "").strip()
    with Session(engine) as session:
        rows = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.ai_role.in_(["digital_member", "assistant_admin"]),
            ).order_by(AssistantAIConfig.id.asc())
        ).all()
    alive = [row for row in rows if str(row.lifecycle_status or "") != "dead"]
    matches = [row for row in alive if str(row.name or "").strip() == wanted]
    if not matches:
        matches = [row for row in alive if str(row.name or "").strip().lower() == wanted.lower()]
    if len(matches) == 1:
        return int(matches[0].id)
    roster = "；".join(f"ID {int(row.id)}={str(row.name or '').strip()}" for row in alive[:50]) or "（无可用成员）"
    if not matches:
        raise HTTPException(
            status_code=400,
            detail=f"找不到名为「{wanted}」的 AI。当前成员：{roster}",
        )
    raise HTTPException(
        status_code=400,
        detail=f"名字「{wanted}」匹配到多个 AI，请改用 to_ai_config_id 指定。当前成员：{roster}",
    )


async def _ai_send_message(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    if ai_config_id is None:
        raise HTTPException(status_code=400, detail="message.send+to targeting an AI must be called by an AI runtime")
    to_raw = args.get("to_ai_config_id") or args.get("target_ai_config_id") or args.get("target")
    to_name = str(args.get("to_ai_name") or args.get("target_ai_name") or args.get("to_name") or "").strip()
    if to_raw is None and not to_name:
        raise HTTPException(status_code=400, detail="to_ai_config_id (或 to_ai_name) is required")
    to_id: Optional[int] = None
    if to_raw is not None:
        try:
            to_id = int(to_raw)
        except Exception:
            # 容错：模型把名字塞进了 to_ai_config_id，按名字解析
            candidate = str(to_raw or "").strip()
            if candidate:
                to_id = _resolve_target_ai_id_by_name(user_id, candidate)
            else:
                raise HTTPException(status_code=400, detail="to_ai_config_id must be an integer")
    if to_id is None:
        to_id = _resolve_target_ai_id_by_name(user_id, to_name)
    if to_id == int(ai_config_id):
        raise HTTPException(status_code=400, detail="cannot send message to self")
    content = str(args.get("content") or args.get("text") or args.get("message") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    require_reply = bool(args.get("require_reply", False))
    timeout_seconds = int(args.get("timeout_seconds") or DEFAULT_REPLY_WAIT_SECONDS)
    message_type = _coerce_message_type(args.get("message_type"))

    sender_ctx = get_run_session_context() or {}
    from_session_id = str(
        args.get("current_session_id")
        or args.get("source_session_id")
        or args.get("from_session_id")
        or args.get("session_id")
        or sender_ctx.get("session_id")
        or ""
    ).strip()
    reply_to_message_id = str(
        args.get("reply_to_message_id")
        or args.get("in_reply_to_message_id")
        or args.get("original_message_id")
        or ""
    ).strip()

    return_route = ai_message_service.find_return_route(
        user_id=user_id,
        current_ai_config_id=int(ai_config_id),
        target_ai_config_id=to_id,
        current_session_id=from_session_id,
    )
    return_session_id = str(return_route.get("from_session_id") or "").strip()

    # 显式 reply_to_message_id：尝试直接落库为对那条消息的回复。
    if reply_to_message_id:
        completed_reply = ai_message_service.resolve_waiting_reply_to_message_id_from_send_message(
            user_id=user_id,
            current_ai_config_id=int(ai_config_id),
            target_ai_config_id=to_id,
            message_id=reply_to_message_id,
            content=content,
        )
        if completed_reply is not None:
            return _reply_result(
                user_id=user_id,
                completed_reply=completed_reply,
                ai_config_id=int(ai_config_id),
                to_id=to_id,
                return_session_id=return_session_id,
            )
        if not return_session_id:
            explicit_route = ai_message_service.find_return_route_by_message_id(
                user_id=user_id,
                current_ai_config_id=int(ai_config_id),
                target_ai_config_id=to_id,
                message_id=reply_to_message_id,
            )
            return_session_id = str(explicit_route.get("from_session_id") or "").strip()
            if explicit_route:
                return_route = explicit_route

    if return_session_id:
        completed_reply = ai_message_service.resolve_waiting_reply_from_send_message(
            user_id=user_id,
            current_ai_config_id=int(ai_config_id),
            target_ai_config_id=to_id,
            current_session_id=from_session_id,
            content=content,
        )
        if completed_reply is not None:
            return _reply_result(
                user_id=user_id,
                completed_reply=completed_reply,
                ai_config_id=int(ai_config_id),
                to_id=to_id,
                return_session_id=return_session_id,
            )

    # 提前确定目标 AI 应该在哪个 session 处理本消息：
    #   - 回信场景   → 优先投回原始发送方 session（from_session_id）
    #   - 点对点信道 → 没有同会话回信路由时，投回“目标 AI 上次与本 AI 交流的那条会话”，
    #                   让 B 事后主动找回 A（哪怕换了上下文）也落在 A 的原始对话里，
    #                   而不是新开一条孤立会话——这才是成员间“一条线”的点对点。
    #   - 普通信件   → 复用“发信方当前会话 ↔ 目标 AI”绑定的目标侧 session
    #   - 新信件     → 生成稳定 session_id，稍后由 wake_idle_target_for_message
    #                   按同一 id 创建会话，pop 时严格匹配。
    if return_session_id:
        prebound_session_id = return_session_id
    else:
        reverse_session_id = ai_message_service.find_reverse_inbound_session(
            user_id=user_id,
            current_ai_config_id=int(ai_config_id),
            target_ai_config_id=to_id,
        )
        if reverse_session_id:
            prebound_session_id = reverse_session_id
        elif from_session_id:
            prebound_session_id = ai_message_service.find_corresponding_target_session_id(
                user_id=user_id,
                from_ai_config_id=int(ai_config_id),
                to_ai_config_id=to_id,
                from_session_id=from_session_id,
            )
        else:
            import uuid as _uuid
            prebound_session_id = f"ai_message_{_uuid.uuid4().hex[:14]}"

    # 保留 cascade_depth 仅用于历史记录兼容，不再作为发送限制。
    parent_depth: Optional[int] = None
    parent_candidate_id = reply_to_message_id or str(return_route.get("message_id") or "").strip()
    if parent_candidate_id:
        parent_row = ai_message_service.fetch_cascade_parent(
            user_id=user_id, message_id=parent_candidate_id
        )
        if parent_row is not None:
            parent_depth = int(getattr(parent_row, "cascade_depth", 0) or 0)
    cascade_depth = (parent_depth + 1) if parent_depth is not None else 0

    try:
        msg = ai_message_service.send(
            user_id=user_id,
            from_ai_config_id=int(ai_config_id),
            to_ai_config_id=to_id,
            content=content,
            target_session_id=prebound_session_id,
            from_session_id=from_session_id,
            require_reply=require_reply,
            timeout_seconds=timeout_seconds,
            message_type=message_type,
            cascade_depth=cascade_depth,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _emit_ai_message_event(user_id, int(ai_config_id), to_id, "message")

    try:
        wakeup = ai_message_service.wake_idle_target_for_message(
            message_id=msg.message_id,
            user_id=user_id,
        )
        if wakeup.get("session_id"):
            msg.target_session_id = str(wakeup.get("session_id") or "")
        target_active = bool(wakeup.get("started") or wakeup.get("run_id"))
    except Exception as exc:
        wakeup = {"started": False, "error": str(exc)}
        target_active = False

    # Always return the actual route selected by the server.  Previously the
    # caller only got message_id, so a first contact that created a new target
    # ChatSession was effectively invisible and a later message could appear to
    # land in a different conversation.
    actual_target_session_id = str(
        (wakeup or {}).get("session_id") or msg.target_session_id or prebound_session_id or ""
    ).strip()
    base_out: Dict[str, Any] = {
        "message_id": msg.message_id,
        "queued": True,
        "to_ai_config_id": to_id,
        "message_type": message_type,
        "require_reply": require_reply,
        "ai_pair_channel_id": ai_message_service.ai_pair_channel_id(
            user_id=user_id,
            ai_config_id_a=int(ai_config_id),
            ai_config_id_b=to_id,
        ),
        "target_session_id": actual_target_session_id,
        "target_session_name": str((wakeup or {}).get("session_name") or "").strip() or None,
    }

    if not require_reply:
        # Fire-and-forget 路径：不阻塞。
        if wakeup and wakeup.get("interrupted"):
            base_out["note"] = "已入队（不等待回复）；目标 AI 当前运行已被打断，系统提示已强制注入并启动新运行。"
        elif return_session_id and wakeup and wakeup.get("started"):
            base_out["note"] = "已入队（不等待回复）；系统已投回原发送方会话并唤醒目标 AI 处理。"
        elif return_session_id:
            base_out["note"] = "已入队（不等待回复）；系统已投回原发送方会话并启动目标 AI 处理。"
        elif wakeup and wakeup.get("started"):
            base_out["note"] = "已入队（不等待回复）；系统已启动目标 AI 处理本消息。"
        elif target_active:
            base_out["note"] = "已入队（不等待回复）；目标 AI 已进入处理队列。"
        else:
            base_out["note"] = "已入队（不等待回复），但目标 AI 唤醒失败。"
        return base_out

    if not target_active:
        # 没人会消费这条消息，立刻返回失败而不是干等到超时。
        base_out["replied"] = False
        base_out["status"] = "failed"
        base_out["failure_reason"] = "target AI is idle and wakeup failed"
        return base_out

    # 事件驱动等待：reply_message 落库后立即唤醒，无轮询、无 5 秒 idle 误判。
    final = await ai_message_service.wait_for_reply(
        message_id=msg.message_id,
        user_id=user_id,
        timeout_seconds=timeout_seconds,
    )
    base_out.update({
        "replied": final.get("status") == "replied",
        "status": final.get("status"),
        "reply_content": final.get("reply_content"),
        "failure_reason": final.get("failure_reason"),
    })
    if final.get("status") == "replied":
        base_out["note"] = "目标 AI 已回复，见 reply_content。"
    elif final.get("status") == "timeout":
        base_out["note"] = f"等待 {timeout_seconds}s 后未收到回复（超时）。"
    else:
        base_out["note"] = "未能拿到回复，详见 status / failure_reason。"
    return base_out


# ---------- 统一入口：message.send+to ----------

_USER_TARGET_ALIASES = {"user", "human", "owner", "用户", "主人", "真人"}

# 旧 message.send+to+ai 调用形态里的显式 AI 寻址参数；出现任意一个即走 AI 分支。
_AI_ADDRESSING_KEYS = (
    "to_ai_config_id",
    "target_ai_config_id",
    "target",
    "to_ai_name",
    "target_ai_name",
    "to_name",
)


async def _send_to(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """message.send+to 统一入口：按 ``to`` 分发给真人用户或另一个 AI。

    - 带 to_ai_config_id / to_ai_name 等旧寻址参数 → AI 分支（兼容旧模板与旧调用习惯）；
    - to 为 "user" 等用户别名，或完全没有寻址参数（旧 send+to+user 形态）→ 用户分支；
    - to 为纯数字 → 按 ai_config_id 发给 AI；其余 → 按成员名字发给 AI。
    """
    if any(args.get(key) not in (None, "") for key in _AI_ADDRESSING_KEYS):
        return await _ai_send_message(user_id, args, ai_config_id)
    to_text = str(args.get("to") or "").strip()
    if not to_text or to_text.lower() in _USER_TARGET_ALIASES:
        return _user_send_message(user_id, args, ai_config_id)
    forwarded = dict(args)
    if to_text.isdigit():
        forwarded["to_ai_config_id"] = int(to_text)
    else:
        forwarded["to_ai_name"] = to_text
    return await _ai_send_message(user_id, forwarded, ai_config_id)
