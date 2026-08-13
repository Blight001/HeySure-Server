"""BotAdapter implementation for Tencent's iLink WeChat bot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlmodel import Session

from api.database import engine
from ._config import WECHAT_DEFAULTS
from ..base import BotAdapter
from ..registry import register

if TYPE_CHECKING:
    from api.models import AssistantAIConfig, ChatMessage
    from ..messaging import MediaPayload, Recipient
    from .routes_store import WeChatRoute


logger = logging.getLogger(__name__)
WECHAT_TEXT_MAX_CHARS = 4000


class WeChatBot(BotAdapter):
    channel = "wechat"
    label = "微信"
    session_prefix = "wechat_"

    def default_config(self) -> Dict[str, Any]:
        return dict(WECHAT_DEFAULTS)

    def is_enabled(self, cfg: "AssistantAIConfig") -> bool:
        return bool(self.read_config(cfg).get("enabled"))

    def has_default_recipient(self, cfg: "AssistantAIConfig") -> bool:
        from .routes_store import latest_wechat_route
        with Session(engine) as session:
            return latest_wechat_route(session, user_id=int(cfg.user_id), ai_config_id=int(cfg.id or 0)) is not None

    def start_long_connections(self) -> int:
        from .long_connection import start_wechat_long_connections
        return start_wechat_long_connections()

    def get_long_connection_state(self, ai_config_id: int, connection_ref: str = "") -> Dict[str, str]:
        from .long_connection import get_wechat_connection_state
        return get_wechat_connection_state(ai_config_id, connection_ref)

    def parse_recipient(self, raw: Dict[str, Any]) -> "Recipient":
        from ..messaging import Recipient
        return Recipient(
            to_id=str((raw or {}).get("to_user_id") or ""),
            to_type=str((raw or {}).get("context_token") or ""),
            connection_ref=str((raw or {}).get("connection_ref") or ""),
        )

    def deliver_text(self, *, user_id: int, ai_config_id: Optional[int], recipient: "Recipient", text: str) -> Any:
        from .service import send_wechat_text
        return send_wechat_text(user_id, ai_config_id, text=text, to_user_id=recipient.to_id, context_token=recipient.to_type, connection_ref=recipient.connection_ref)

    def deliver_media(self, *, user_id: int, ai_config_id: Optional[int], recipient: "Recipient", media: "MediaPayload") -> Any:
        from .service import send_wechat_media
        return send_wechat_media(
            user_id,
            ai_config_id,
            media=media,
            to_user_id=recipient.to_id,
            context_token=recipient.to_type,
            connection_ref=recipient.connection_ref,
        )

    def normalize_text(self, text: str, *, strip_markdown: bool = True) -> str:
        from .service import normalize_wechat_text
        return normalize_wechat_text(text, strip_markdown=strip_markdown)

    def load_session_route(self, session: "Session", message: "ChatMessage") -> Optional["WeChatRoute"]:
        from .routes_store import load_wechat_route
        return load_wechat_route(session, message)

    def notify_assistant_message(self, session: "Session", message: "ChatMessage", *, rendered_content: str, route: Any) -> None:
        from .service import send_wechat_text
        for start in range(0, len(rendered_content), WECHAT_TEXT_MAX_CHARS):
            chunk = rendered_content[start:start + WECHAT_TEXT_MAX_CHARS].strip()
            if not chunk:
                continue
            try:
                send_wechat_text(
                    int(message.user_id),
                    int(message.ai_config_id or 0),
                    text=chunk,
                    to_user_id=str(route.to_user_id),
                    context_token=str(route.context_token),
                    connection_ref=str(route.connection_ref),
                )
            except Exception as exc:
                logger.warning("wechat reply failed message_id=%s error_type=%s", message.id, type(exc).__name__)
                return

    def diagnose(self, cfg: "AssistantAIConfig", *, user_id: int) -> Dict[str, Any]:
        state = self.get_long_connection_state(int(cfg.id or 0))
        return {"ok": state.get("status") == "success", "supported": True, "connection_mode": "ilink", "bot_status": state, "status": state.get("status")}

    def extra_required_mcp_tools(self) -> set:
        return {"conversation.manage"}

    def build_status(self, cfg: "AssistantAIConfig", *, remote_state: Optional[Dict[str, str]] = None, remote_error: Optional[str] = None) -> Dict[str, str]:
        from .. import status
        if not self.read_config(cfg).get("enabled"):
            return status.disabled("微信机器人未启用")
        if remote_error:
            return status.failed("ilink", "Connector Runtime 状态不可用")
        state = remote_state or self.get_long_connection_state(int(cfg.id or 0))
        if str(state.get("status") or "") == "starting":
            return status.status_report("starting", "ilink", str(state.get("message") or ""))
        return status.from_connection_state(state, mode="ilink", starting_hint="请扫码连接微信机器人")

    def start_login(self, config_id: int, user_id: int, connection_ref: str = "") -> Dict[str, Any]:
        from .login import manager
        return manager.start(config_id, user_id, connection_ref)

    def login_status(self, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        from .login import manager
        return manager.snapshot(config_id, connection_ref)

    def submit_login_verify_code(self, config_id: int, value: str, connection_ref: str = "") -> Dict[str, Any]:
        from .login import manager
        return manager.submit_verify_code(config_id, value, connection_ref)

    def logout(self, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        from .login import manager
        return manager.logout(config_id, connection_ref)


register(WeChatBot())
