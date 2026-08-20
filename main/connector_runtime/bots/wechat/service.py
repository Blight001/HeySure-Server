"""Outbound text delivery for Tencent iLink WeChat bots."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_credentials import decrypt_credentials
from connector_runtime.bots.text_format import strip_markdown_to_plain
from connector_runtime.bots.messaging import MediaPayload
from .ilink_client import ILinkClient, requests
from .routes_store import latest_wechat_route


logger = logging.getLogger(__name__)
WECHAT_SEND_ATTEMPTS = 2
WECHAT_SEND_RETRY_DELAY_SECONDS = 0.25


@dataclass(frozen=True)
class _SendAttempt:
    success: bool
    retryable: bool = False
    reason: str = ""
    error: Optional[HTTPException] = None


def _send_text_attempt(client: ILinkClient, *, to_user_id: str, context_token: str, text: str, client_id: str) -> _SendAttempt:
    try:
        result = client.send_text(
            to_user_id=to_user_id,
            context_token=context_token,
            text=text,
            client_id=client_id,
        )
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        retryable = status is None or status == 429 or int(status) >= 500
        safe_status = int(status) if status is not None else 502
        error = HTTPException(status_code=502, detail=f"微信发送请求失败（HTTP {safe_status}）")
        return _SendAttempt(False, retryable, f"http status={status or 'network'}", error)
    ret = int(result.get("ret") or 0)
    if ret == 0:
        return _SendAttempt(True)
    error = HTTPException(status_code=502, detail=f"微信消息发送失败（ret={ret}）")
    return _SendAttempt(False, True, f"provider ret={ret}", error)


def _send_text_with_retry(client: ILinkClient, *, to_user_id: str, context_token: str, text: str) -> Dict[str, Any]:
    client_id = f"heysure-wechat-{uuid.uuid4().hex}"
    for attempt in range(1, WECHAT_SEND_ATTEMPTS + 1):
        outcome = _send_text_attempt(
            client,
            to_user_id=to_user_id,
            context_token=context_token,
            text=text,
            client_id=client_id,
        )
        if outcome.success:
            return {"success": True}
        if not outcome.retryable or attempt >= WECHAT_SEND_ATTEMPTS:
            raise outcome.error or HTTPException(status_code=502, detail="微信消息发送失败")
        logger.warning("wechat send retry attempt=%s reason=%s", attempt, outcome.reason)
        time.sleep(WECHAT_SEND_RETRY_DELAY_SECONDS)
    raise HTTPException(status_code=502, detail="微信消息发送失败")


def _connection(user_id: int, ai_config_id: int, connection_ref: str = "") -> tuple[BotConnection, str]:
    with Session(engine) as session:
        stmt = select(BotConnection).where(
            BotConnection.channel == "wechat",
            BotConnection.user_id == user_id,
            BotConnection.ai_config_id == ai_config_id,
        )
        if connection_ref:
            stmt = stmt.where(BotConnection.connection_ref == connection_ref)
        row = session.exec(stmt.order_by(BotConnection.is_default.desc())).first()
    if row is None or row.state != "connected":
        raise HTTPException(status_code=409, detail="微信机器人尚未连接")
    token = str(decrypt_credentials(row.credentials_encrypted).get("bot_token") or "")
    if not token:
        raise HTTPException(status_code=409, detail="微信机器人连接凭据不可用")
    return row, token


def send_wechat_text(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    text: str,
    to_user_id: str = "",
    context_token: str = "",
    connection_ref: str = "",
) -> Dict[str, Any]:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="微信发送需要 ai_config_id")
    if not to_user_id or not context_token:
        with Session(engine) as session:
            route = latest_wechat_route(session, user_id=user_id, ai_config_id=int(ai_config_id), connection_ref=connection_ref)
        if route:
            to_user_id = to_user_id or route.to_user_id
            context_token = context_token or route.context_token
            connection_ref = connection_ref or route.connection_ref
    if not to_user_id or not context_token:
        raise HTTPException(status_code=409, detail="尚无可回复的微信私聊会话")
    row, token = _connection(user_id, int(ai_config_id), connection_ref)
    client = ILinkClient(base_url=row.base_url, token=token)
    return _send_text_with_retry(
        client,
        to_user_id=to_user_id,
        context_token=context_token,
        text=str(text or ""),
    )


def send_wechat_media(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    media: MediaPayload,
    to_user_id: str = "",
    context_token: str = "",
    connection_ref: str = "",
) -> Dict[str, Any]:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="微信发送需要 ai_config_id")
    if not media.path and not media.url:
        raise HTTPException(status_code=400, detail="微信媒体发送需要文件路径或 URL")
    if not to_user_id or not context_token:
        with Session(engine) as session:
            route = latest_wechat_route(session, user_id=user_id, ai_config_id=int(ai_config_id), connection_ref=connection_ref)
        if route:
            to_user_id = to_user_id or route.to_user_id
            context_token = context_token or route.context_token
            connection_ref = connection_ref or route.connection_ref
    if not to_user_id or not context_token:
        raise HTTPException(status_code=409, detail="尚无可回复的微信私聊会话")
    row, token = _connection(user_id, int(ai_config_id), connection_ref)
    from .media import send_media
    return send_media(
        ILinkClient(base_url=row.base_url, token=token),
        to_user_id=to_user_id,
        context_token=context_token,
        text=normalize_wechat_text(media.text),
        path=media.path,
        url=media.url,
        media_type=media.media_type,
        file_name=media.file_name,
    )


def normalize_wechat_text(text: str, *, strip_markdown: bool = True) -> str:
    return strip_markdown_to_plain(text, collapse_tables=True) if strip_markdown else str(text or "").strip()
