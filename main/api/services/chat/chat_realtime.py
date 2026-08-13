"""Best-effort browser notifications for externally persisted chat messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from api.sio import ui_sio


logger = logging.getLogger(__name__)


async def emit_history_changed(
    *,
    user_id: int,
    session_id: str,
    ai_config_id: Optional[int],
    ai_kind: str,
    message_id: int,
    source: str,
) -> None:
    """Tell the gateway-owned browser room to pull the new committed tail."""
    try:
        await ui_sio.emit(
            "chat:history_changed",
            {
                "action": "append",
                "source": str(source or "external"),
                "user_id": int(user_id),
                "session_id": str(session_id or "default"),
                "ai_config_id": ai_config_id,
                "ai_kind": str(ai_kind or "assistant"),
                "message_id": int(message_id),
            },
            room=f"user_{int(user_id)}",
        )
    except Exception:
        # Realtime delivery is an acceleration path. The committed message and
        # the web client's incremental polling remain the source of truth.
        logger.exception("chat history notification failed source=%s", source)


def notify_history_changed(**kwargs) -> None:
    """Run the async relay from synchronous connector worker threads."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(emit_history_changed(**kwargs))
        return
    loop.create_task(emit_history_changed(**kwargs))
