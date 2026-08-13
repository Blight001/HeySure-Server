"""Translate iLink private messages into HeySure chat runs."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig, BotConnection, ChatMessage, ChatMessageCreate, ChatSession, User
from api.services.chat.chat_persistence import _save_message
from connector_runtime.bots.commands import handle_bot_command
from connector_runtime.bots.session_cursor import get_active_session_id
from .routes_store import register_wechat_route
from .service import send_wechat_text
from .worker import active_run, launch_message_run


BUSY_REPLY = "稍等，AI 正在处理上一条消息。"


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    sender: str
    context_token: str
    provider_message_id: str
    items: tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class PreparedMessage:
    user_id: int
    ai_kind: str
    session_id: str
    session_name: str
    message_id: int
    active: bool
    model_content: Any


def _message_text(message: Dict[str, Any]) -> str:
    parts = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = int(item.get("type") or 0)
        content = item.get("text_item") if item_type == 1 else item.get("voice_item") if item_type == 3 else None
        value = str(content.get("text") or "").strip() if isinstance(content, dict) else ""
        if value:
            parts.append(value)
    return "\n".join(parts).strip()


def _has_supported_media(items: tuple[Dict[str, Any], ...]) -> bool:
    return any(int(item.get("type") or 0) in {2, 3, 4, 5} for item in items)


def _parse_incoming(message: Dict[str, Any]) -> Optional[IncomingMessage]:
    if int(message.get("message_type") or 0) != 1 or str(message.get("group_id") or ""):
        return None
    items = tuple(item for item in (message.get("item_list") or []) if isinstance(item, dict))
    incoming = IncomingMessage(
        text=_message_text(message),
        sender=str(message.get("from_user_id") or "").strip(),
        context_token=str(message.get("context_token") or "").strip(),
        provider_message_id=str(message.get("message_id") or "").strip(),
        items=items,
    )
    usable = bool(incoming.text or _has_supported_media(items))
    return incoming if usable and incoming.sender and incoming.context_token else None


def _enabled_config(session: Session, config_id: int, connection_ref: str) -> Optional[AssistantAIConfig]:
    cfg = session.get(AssistantAIConfig, config_id)
    connection = session.exec(select(BotConnection).where(
        BotConnection.ai_config_id == config_id,
        BotConnection.channel == "wechat",
        BotConnection.connection_ref == connection_ref,
        BotConnection.enabled.is_(True),
        BotConnection.state == "connected",
    )).first()
    if not cfg or not connection:
        return None
    return cfg


def _session_identity(session: Session, cfg: AssistantAIConfig, config_id: int, incoming: IncomingMessage, connection_ref: str) -> tuple[str, str, str]:
    ai_kind = "assistant" if cfg.ai_role == "assistant_admin" else "core"
    home_session_id = f"wechat_{config_id}_{connection_ref}_{incoming.sender}"
    session_id = get_active_session_id(
        session,
        channel="wechat",
        user_id=int(cfg.user_id),
        ai_config_id=config_id,
        ai_kind=ai_kind,
        identity_key=f"{connection_ref}:{incoming.sender}",
        default=home_session_id,
    )
    row = session.exec(select(ChatSession).where(
        ChatSession.user_id == cfg.user_id,
        ChatSession.ai_config_id == config_id,
        ChatSession.ai_kind == ai_kind,
        ChatSession.session_id == session_id,
    )).first()
    session_name = str(row.session_name or "") if row else ""
    return ai_kind, session_id, session_name or f"微信对话 {incoming.sender[-8:]}"


def _duplicate_inbound(session: Session, cfg: AssistantAIConfig, ai_kind: str, session_id: str, tag: str, has_provider_id: bool) -> bool:
    if not has_provider_id:
        return False
    return session.exec(select(ChatMessage).where(
        ChatMessage.user_id == cfg.user_id,
        ChatMessage.ai_config_id == cfg.id,
        ChatMessage.ai_kind == ai_kind,
        ChatMessage.session_id == session_id,
        ChatMessage.tags == tag,
    )).first() is not None


def _handle_command(
    session: Session,
    cfg: AssistantAIConfig,
    incoming: IncomingMessage,
    *,
    ai_kind: str,
    session_id: str,
    session_name: str,
    home_session_id: str,
    connection_ref: str,
) -> bool:
    if not incoming.text or _has_supported_media(incoming.items):
        return False
    user = session.get(User, cfg.user_id)
    if not user:
        return True
    result = handle_bot_command(
        session,
        text=incoming.text,
        channel="wechat",
        user=user,
        cfg=cfg,
        ai_kind=ai_kind,
        identity_key=f"{connection_ref}:{incoming.sender}",
        current_session_id=session_id,
        current_session_name=session_name,
        home_session_id=home_session_id,
    )
    if result is None:
        return False
    send_wechat_text(
        int(cfg.user_id), int(cfg.id or 0), text=result.text,
        to_user_id=incoming.sender, context_token=incoming.context_token,
        connection_ref=connection_ref,
    )
    return True


def _prepare_message(config_id: int, incoming: IncomingMessage, connection_ref: str) -> PreparedMessage | Dict[str, Any]:
    with Session(engine) as session:
        cfg = _enabled_config(session, config_id, connection_ref)
        if cfg is None:
            return {"success": True, "ignored": True}
        ai_kind, session_id, session_name = _session_identity(session, cfg, config_id, incoming, connection_ref)
        home_session_id = f"wechat_{config_id}_{connection_ref}_{incoming.sender}"
        register_wechat_route(
            session,
            user_id=int(cfg.user_id),
            ai_config_id=config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            to_user_id=incoming.sender,
            context_token=incoming.context_token,
            connection_ref=connection_ref,
        )
        tag = f"wechat_inbound:{incoming.provider_message_id}" if incoming.provider_message_id else "wechat_inbound"
        if _duplicate_inbound(session, cfg, ai_kind, session_id, tag, bool(incoming.provider_message_id)):
            return {"success": True, "duplicate": True}
        if _handle_command(
            session, cfg, incoming, ai_kind=ai_kind, session_id=session_id,
            session_name=session_name, home_session_id=home_session_id,
            connection_ref=connection_ref,
        ):
            return {"success": True, "command_handled": True}
        content = incoming.text or "用户发送了微信媒体消息。"
        inbound = _save_message(session, int(cfg.user_id), ChatMessageCreate(
            role="user",
            content=content,
            ai_config_id=config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            tags=tag,
            total_tokens=0,
        ))
        from .inbound_media import bind_wechat_media
        model_content = bind_wechat_media(
            session,
            message_id=int(inbound.id or 0),
            user_id=int(cfg.user_id),
            ai_config_id=config_id,
            text=content,
            items=incoming.items,
        )
        active = active_run(
            session, user_id=int(cfg.user_id), config_id=config_id,
            ai_kind=ai_kind, session_id=session_id,
        )
        return PreparedMessage(
            user_id=int(cfg.user_id),
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            message_id=int(inbound.id or 0),
            active=active is not None,
            model_content=model_content,
        )


def handle_wechat_message(config_id: int, message: Dict[str, Any], *, connection_ref: str = "") -> Dict[str, Any]:
    if not connection_ref:
        with Session(engine) as session:
            row = session.exec(select(BotConnection).where(
                BotConnection.ai_config_id == config_id,
                BotConnection.channel == "wechat",
                BotConnection.enabled.is_(True),
            ).order_by(BotConnection.is_default.desc())).first()
            connection_ref = row.connection_ref if row else ""
    incoming = _parse_incoming(message)
    if incoming is None:
        return {"success": True, "ignored": True}
    prepared = _prepare_message(config_id, incoming, connection_ref)
    if isinstance(prepared, dict):
        return prepared
    kwargs = {
        "user_id": prepared.user_id,
        "config_id": config_id,
        "ai_kind": prepared.ai_kind,
        "session_id": prepared.session_id,
        "session_name": prepared.session_name,
        "message_id": prepared.message_id,
        "model_content": prepared.model_content,
    }
    if prepared.active:
        send_wechat_text(
            prepared.user_id, config_id, text=BUSY_REPLY,
            to_user_id=incoming.sender, context_token=incoming.context_token,
            connection_ref=connection_ref,
        )
        threading.Thread(
            target=launch_message_run,
            kwargs={**kwargs, "wait_for_idle": True},
            daemon=True,
        ).start()
        _notify_prepared_message(prepared, config_id)
        return {"success": True, "queued_after_active": True}
    run_id = launch_message_run(**kwargs, wait_for_idle=False)
    _notify_prepared_message(prepared, config_id)
    return {"success": True, "run_id": run_id}


def _notify_prepared_message(prepared: PreparedMessage, config_id: int) -> None:
    # Publish only after the ChatRun row exists. The browser can then adopt the
    # externally-started run immediately instead of racing the connector.
    from api.services.chat.chat_realtime import notify_history_changed

    notify_history_changed(
        user_id=prepared.user_id,
        session_id=prepared.session_id,
        ai_config_id=config_id,
        ai_kind=prepared.ai_kind,
        message_id=prepared.message_id,
        source="wechat",
    )
