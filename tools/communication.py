"""通信类 MCP 工具：
- message.send_to_user → 向用户发送消息（按 AI 配置选择对应机器人插件）
- message.send_to_ai   → 向另一个 AI 发送消息。所有"回信"都走它本身：带
                       message_type="reply" 与 reply_to_message_id。
                       系统按 (target_session_id, status) 严格匹配。
"""

import os
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session

from connector_runtime.bots.messaging import MediaPayload, dispatcher
from api.database import engine
from mcp_runtime.mcp.core import get_project_root, safe_join
from api.models import User
from ai_runtime.inference import ai_message_service
from connector_runtime.dispatch.device_dispatch import get_run_session_context


_ALLOWED_MESSAGE_TYPES = {"inquiry", "reply", "chitchat", "notify"}
_MESSAGE_TYPE_HINT = (
    'message_type is required. Use "inquiry" for a question/request that expects an answer, '
    '"reply" for answering a previous inquiry, "notify" for one-way notification/status/result '
    'that does not expect an answer, or "chitchat" for casual multi-turn chat.'
)
DEFAULT_REPLY_WAIT_SECONDS = 24 * 60 * 60


def _resolve_server_media_path(user_id: int, ai_config_id: Optional[int], media_path: str) -> str:
    value = str(media_path or "").strip()
    if not value:
        return ""
    if os.path.isabs(value):
        return value
    root = get_project_root(user_id, ai_config_id)
    return safe_join(root, value.replace("\\", "/"))


def _coerce_message_type(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in _ALLOWED_MESSAGE_TYPES:
        return text
    raise HTTPException(status_code=400, detail=_MESSAGE_TYPE_HINT)


# ---------- 与用户通信 ----------

def _user_send_message(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """主动向用户推送一条消息。当前支持：飞书机器人、QQ机器人。"""
    text = str(args.get("text") or args.get("content") or args.get("message") or "").strip()
    media_url = str(
        args.get("media_url")
        or args.get("image_url")
        or args.get("video_url")
        or args.get("file_url")
        or ""
    ).strip()
    media_path = str(
        args.get("media_path")
        or args.get("image_path")
        or args.get("video_path")
        or args.get("file_path")
        or ""
    ).strip()
    media_path = _resolve_server_media_path(user_id, ai_config_id, media_path)
    media_type = str(args.get("media_type") or ("image" if (args.get("image_url") or args.get("image_path")) else "") or ("video" if (args.get("video_url") or args.get("video_path")) else "")).strip()
    file_name = str(args.get("file_name") or args.get("filename") or "").strip()
    if not text and not media_url and not media_path:
        raise HTTPException(status_code=400, detail="text or media_url/media_path is required for message.send_to_user")
    channel = str(args.get("channel") or "").strip().lower()

    # The whole arg bag is handed to the dispatcher as the raw addressing
    # payload; each channel's adapter (parse_recipient) picks the aliases it
    # understands. Channel resolution + default-receiver fallback live in the
    # dispatcher / adapter, not here.
    if media_url or media_path:
        delivery = dispatcher.send_media(
            user_id=user_id,
            ai_config_id=ai_config_id,
            channel=channel or None,
            media=MediaPayload(
                text=text,
                url=media_url,
                path=media_path,
                media_type=media_type,
                file_name=file_name,
                duration=args.get("duration"),
            ),
            raw_target=args,
        )
    else:
        delivery = dispatcher.send_text(
            user_id=user_id,
            ai_config_id=ai_config_id,
            channel=channel or None,
            text=text,
            raw_target=args,
        )
    channel = delivery.channel
    result = delivery.detail
    # 套一层 user 语义包装 + 拼出"已送达"提示
    notice_template = ""
    try:
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user:
                from api.services import kb_store

                notice_template = kb_store.effective_system_value(
                    user_id, "prompt_user_message_notice",
                    getattr(user, "prompt_user_message_notice", ""),
                )
    except Exception:
        notice_template = ""
    notice = ""
    if notice_template:
        try:
            notice = notice_template.format(channel=channel)
        except Exception:
            notice = notice_template

    out: Dict[str, Any] = {
        "delivered": True,
        "channel": channel,
    }
    # 底层机器人返回里若带消息 id，保留一个轻量引用即可，不回吐整包响应。
    if isinstance(result, dict):
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        sent_id = result.get("message_id") or result.get("msg_id") or data.get("message_id")
        if sent_id:
            out["message_id"] = sent_id
    if notice:
        out["notice"] = notice
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


async def _ai_send_message(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    if ai_config_id is None:
        raise HTTPException(status_code=400, detail="message.send_to_ai must be called by an AI runtime")
    to_raw = args.get("to_ai_config_id") or args.get("target_ai_config_id") or args.get("target")
    if to_raw is None:
        raise HTTPException(status_code=400, detail="to_ai_config_id is required")
    try:
        to_id = int(to_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="to_ai_config_id must be an integer")
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
    #   - 普通信件   → 复用“发信方当前会话 ↔ 目标 AI”绑定的目标侧 session
    #   - 新信件     → 生成稳定 session_id，稍后由 wake_idle_target_for_message
    #                   按同一 id 创建会话，pop 时严格匹配。
    if return_session_id:
        prebound_session_id = return_session_id
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

    base_out: Dict[str, Any] = {
        "message_id": msg.message_id,
        "queued": True,
        "to_ai_config_id": to_id,
        "message_type": message_type,
        "require_reply": require_reply,
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


