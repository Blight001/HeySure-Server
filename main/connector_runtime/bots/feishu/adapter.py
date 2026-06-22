"""Feishu (Lark) bot adapter.

Wraps the existing service / long-connection helpers behind the uniform
:class:`BotAdapter` interface. The adapter is a thin façade — the real
work continues to live in ``service.py`` / ``long_connection.py`` and the
``router.py`` HTTP handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..base import BotAdapter
from ..registry import register
from ._config import FEISHU_DEFAULTS


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlmodel import Session

    from api.models import AssistantAIConfig, ChatMessage
    from ..messaging import MediaPayload, Recipient
    from .routes_store import FeishuRouteView


# Feishu's open API caps single text messages at ~2KB; we chunk well below
# that to leave room for the assistant-prefix icons we glue on the front.
FEISHU_TEXT_MAX_CHARS = 1800


class FeishuBot(BotAdapter):
    channel = "feishu"
    label = "飞书"
    session_prefix = "feishu_"

    # ---- config -----------------------------------------------------------

    def default_config(self) -> dict:
        return dict(FEISHU_DEFAULTS)

    # ---- enablement --------------------------------------------------------

    def is_enabled(self, cfg: "AssistantAIConfig") -> bool:
        channel = str(getattr(cfg, "bot_channel", "") or "feishu").strip().lower()
        if channel != self.channel:
            return False
        return bool(self.read_config(cfg).get("enabled"))

    def has_default_recipient(self, cfg: "AssistantAIConfig") -> bool:
        bot_cfg = self.read_config(cfg)
        # A default receive_id, or a webhook-only "仅通知" URL, both give the
        # forward somewhere to land.
        return bool(
            str(bot_cfg.get("default_receive_id") or "").strip()
            or str(bot_cfg.get("webhook_url") or "").strip()
        )

    # ---- long-connection lifecycle ----------------------------------------

    def start_long_connections(self) -> int:
        from .long_connection import start_feishu_long_connection_clients
        return start_feishu_long_connection_clients()

    def get_long_connection_state(self, ai_config_id: int) -> Dict[str, str]:
        from .long_connection import get_feishu_long_connection_state
        return get_feishu_long_connection_state(ai_config_id)

    # ---- outbound messaging -----------------------------------------------

    def parse_recipient(self, raw: Dict[str, Any]) -> "Recipient":
        from ..messaging import Recipient

        raw = raw or {}
        to_id = (
            raw.get("receive_id") or raw.get("chat_id")
            or raw.get("open_id") or raw.get("to_id") or ""
        )
        to_type = raw.get("receive_id_type") or raw.get("to_type")
        if not to_type and raw.get("open_id"):
            to_type = "open_id"
        return Recipient(to_id=str(to_id).strip(), to_type=str(to_type or "").strip())

    def deliver_text(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        recipient: "Recipient",
        text: str,
    ) -> Any:
        from .service import send_feishu_text_message
        return send_feishu_text_message(
            user_id,
            ai_config_id,
            text=text,
            receive_id=recipient.to_id,
            receive_id_type=recipient.to_type,
        )

    def deliver_media(
        self,
        *,
        user_id: int,
        ai_config_id: Optional[int],
        recipient: "Recipient",
        media: "MediaPayload",
    ) -> Any:
        from .service import send_feishu_media_message, send_feishu_text_message

        results = []
        # Feishu requires text and media to ride on separate messages.
        if media.text and media.has_media:
            results.append(
                send_feishu_text_message(
                    user_id,
                    ai_config_id,
                    text=media.text,
                    receive_id=recipient.to_id,
                    receive_id_type=recipient.to_type,
                )
            )
        results.append(
            send_feishu_media_message(
                user_id,
                ai_config_id,
                media_url=media.url,
                media_path=media.path,
                media_type=media.media_type,
                file_name=media.file_name,
                receive_id=recipient.to_id,
                receive_id_type=recipient.to_type,
                duration=int(media.duration) if media.duration is not None else None,
            )
        )
        if len(results) > 1:
            return {"success": True, "results": results}
        return results[-1]

    def normalize_text(self, text: str, *, strip_markdown: bool = True) -> str:
        from .service import normalize_feishu_text
        return normalize_feishu_text(text, strip_markdown=strip_markdown)

    # ---- notify dispatch --------------------------------------------------

    def load_session_route(
        self, session: "Session", message: "ChatMessage"
    ) -> Optional["FeishuRouteView"]:
        from .routes_store import load_feishu_route
        return load_feishu_route(session, message)

    def notify_assistant_message(
        self,
        session: "Session",
        message: "ChatMessage",
        *,
        rendered_content: str,
        route: Any,
    ) -> None:
        from .service import send_feishu_text_message

        receive_id = str(getattr(route, "receive_id", "") or "")
        receive_id_type = str(getattr(route, "receive_id_type", "") or "chat_id")
        for start in range(0, len(rendered_content), FEISHU_TEXT_MAX_CHARS):
            chunk = rendered_content[start:start + FEISHU_TEXT_MAX_CHARS].strip()
            if not chunk:
                continue
            try:
                send_feishu_text_message(
                    int(message.user_id),
                    int(message.ai_config_id or 0),
                    text=chunk,
                    receive_id=receive_id,
                    receive_id_type=receive_id_type,
                )
            except Exception as exc:
                logger.exception(f"send failed message_id={message.id}: {exc}")
                return

    # ---- diagnostics ------------------------------------------------------

    def diagnose(self, cfg: "AssistantAIConfig", *, user_id: int) -> Dict[str, Any]:
        """End-to-end check: config completeness + access-token fetch + state."""
        from .long_connection import get_feishu_long_connection_state
        from .service import get_tenant_access_token

        bot_cfg = self.read_config(cfg)
        app_id = str(bot_cfg.get("app_id") or "").strip()
        app_secret = str(bot_cfg.get("app_secret") or "").strip()
        webhook_url = str(bot_cfg.get("webhook_url") or "").strip()

        out: Dict[str, Any] = {
            "supported": True,
            "ai_config_id": int(cfg.id or 0),
            "bot_channel": str(cfg.bot_channel or ""),
            "feishu_enabled": bool(bot_cfg.get("enabled")),
            "app_id_configured": bool(app_id),
            "app_secret_configured": bool(app_secret),
            "webhook_only": bool(webhook_url and not (app_id and app_secret)),
            "default_receive_id_configured": bool(str(bot_cfg.get("default_receive_id") or "").strip()),
            "default_receive_id_type": str(bot_cfg.get("default_receive_id_type") or "chat_id"),
            "callback_path": f"/api/feishu/events/{int(cfg.id or 0)}",
            "connection_mode": "lark_websocket" if (app_id and app_secret) else ("webhook" if webhook_url else "none"),
        }

        if app_id and app_secret:
            try:
                token = get_tenant_access_token(user_id, int(cfg.id or 0))
                out["token_ok"] = bool(token)
                out["token_preview"] = f"{token[:6]}..." if token else ""
            except Exception as exc:
                out["token_ok"] = False
                out["error"] = str(exc)
            bot_state = get_feishu_long_connection_state(int(cfg.id or 0))
            out["bot_status"] = bot_state
            out["status"] = bot_state.get("status") or "failed"
            out["ok"] = bool(out.get("token_ok") and bot_state.get("status") == "success")
        elif webhook_url:
            out["bot_status"] = {"status": "success", "message": "webhook-only mode"}
            out["status"] = "success"
            out["ok"] = True
        else:
            out["bot_status"] = {"status": "failed", "message": "未配置 App ID/Secret 或 webhook"}
            out["status"] = "failed"
            out["ok"] = False
        return out

    # ---- runtime tool requirements ---------------------------------------

    def extra_required_mcp_tools(self) -> set:
        # Feishu conversations need a self-service context-trim path plus the
        # shared-pool multi-session tools, even for older AI configs whose
        # saved MCP allowlist predates them.
        return {
            "conversation.manage",
        }

    # ---- status -----------------------------------------------------------

    def build_status(
        self,
        cfg: "AssistantAIConfig",
        *,
        remote_state: Optional[Dict[str, str]] = None,
        remote_error: Optional[str] = None,
    ) -> Dict[str, str]:
        from .. import status

        if str(cfg.bot_channel or "feishu").strip().lower() != self.channel:
            return status.disabled("当前机器人类型不是飞书")
        bot_cfg = self.read_config(cfg)
        if not bot_cfg.get("enabled"):
            return status.disabled("飞书机器人未启用")
        app_id = str(bot_cfg.get("app_id") or "").strip()
        app_secret = str(bot_cfg.get("app_secret") or "").strip()
        webhook_url = str(bot_cfg.get("webhook_url") or "").strip()
        if app_id or app_secret:
            if not app_id or not app_secret:
                return status.failed("long_connection", "App ID / Secret 配置不完整")
            state = remote_state
            if state is None:
                if remote_error:
                    return status.failed("long_connection", f"connector-runtime 状态不可用: {remote_error}")
                state = self.get_long_connection_state(int(cfg.id or 0))
            return status.from_connection_state(state, mode="long_connection", starting_hint="")
        if webhook_url:
            return status.status_report("success", "webhook", "仅通知发送配置已完成")
        return status.failed("none", "未配置 App ID/Secret 或 仅通知 URL")


# Self-register the singleton at import time.
register(FeishuBot())
