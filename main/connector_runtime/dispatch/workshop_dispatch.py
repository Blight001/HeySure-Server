"""Inline execution for server-owned built-in device tools."""

import asyncio
import logging
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


async def execute_workshop_inline(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    args: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    from fastapi import HTTPException
    from library import engine as workshop_engine

    device_id = workshop_engine.device_id_for_user(user_id)
    try:
        result = await asyncio.to_thread(
            workshop_engine.execute_tool,
            user_id,
            ai_config_id,
            tool,
            dict(args or {}),
        )
        return {
            "success": True,
            "deviceId": device_id,
            "tool": tool,
            "summary": "",
            "result": result,
        }
    except HTTPException as exc:
        return {
            "success": False,
            "deviceId": device_id,
            "tool": tool,
            "error": str(exc.detail),
        }
    except Exception as exc:
        logger.exception("built-in device tool failed tool=%s user=%s", tool, user_id)
        return {
            "success": False,
            "deviceId": device_id,
            "tool": tool,
            "error": str(exc),
        }
