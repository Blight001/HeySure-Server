"""Normalize endpoint results and persist browser cookie snapshots."""

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from api.database import engine
from api.services.mcp.mcp_tool_media import canonical_screenshot_tool_name


logger = logging.getLogger(__name__)
IMAGE_DATA_URL_KEYS = {"dataUrl", "data_url", "imageDataUrl", "screenshotDataUrl", "screenshot"}


def explicit_send_disabled(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    return any(
        key in args and args.get(key) is False
        for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    )


def should_send_screenshot_to_user(tool: str, result: Any, args: Any = None) -> bool:
    if explicit_send_disabled(args):
        return False
    tool_kind = canonical_screenshot_tool_name(tool)
    requested = isinstance(args, dict) and any(
        args.get(key) is True for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    )
    result_requested = isinstance(result, dict) and any(
        result.get(key) is True for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    )
    return requested or result_requested or tool_kind in {
        "vision_capture", "vision_capture_mouse", "screen_capture", "screen_capture_region"
    }


def normalize_screenshot_result_for_delivery(tool: str, result: Any, args: Any = None) -> Any:
    if not isinstance(result, dict) or not should_send_screenshot_to_user(tool, result, args):
        return result
    normalized = dict(result)
    normalized["send_to_user"] = True
    normalized["save_to_server"] = True
    return normalized


def omit_screenshot_bytes(value: Any) -> Any:
    if isinstance(value, list):
        return [omit_screenshot_bytes(item) for item in value]
    if not isinstance(value, dict):
        return value
    output: Dict[str, Any] = {}
    for key, item in value.items():
        if key in IMAGE_DATA_URL_KEYS and isinstance(item, str) and item.startswith("data:image/"):
            output[key] = f"<image data URL omitted, {len(item)} chars>"
        elif key in {"server_path", "workspace_path"}:
            output[key] = item
        else:
            output[key] = omit_screenshot_bytes(item)
    return output


def _workspace_dir(user_id: int, ai_config_id: Optional[int]) -> str:
    from api.core.config import ai_workspace_dirname, user_workspace_dir

    user_root = user_workspace_dir(user_id)
    if not ai_config_id:
        return user_root
    try:
        from api.models import AssistantAIConfig

        with Session(engine) as session:
            config = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.id == ai_config_id,
                    AssistantAIConfig.user_id == user_id,
                )
            ).first()
        if not config:
            return os.path.abspath(os.path.join(user_root, f"{ai_config_id}-ai"))
        role = str(config.ai_role or "").strip().lower()
        member_role = str(config.digital_member_role or "").strip().lower()
        if role == "digital_member" and member_role == "manager":
            return user_root
        return os.path.abspath(
            os.path.join(user_root, ai_workspace_dirname(config.id, config.name, config.ai_role))
        )
    except Exception:
        return os.path.abspath(os.path.join(user_root, f"{ai_config_id}-ai"))


def _cookie_payload(result: dict, data: dict, cookies: Any, browser_storage: Any) -> dict:
    if data.get("cookies"):
        return data
    return {
        "account": data.get("account"),
        "password": data.get("password"),
        "pageUrl": result.get("pageUrl") or data.get("pageUrl"),
        "pageTitle": result.get("pageTitle") or data.get("pageTitle"),
        "cookies": cookies or data.get("cookies"),
        "browserStorage": browser_storage or data.get("browserStorage"),
        "capturedAt": data.get("capturedAt") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "save_cookies_mcp",
    }


def _clean_result(result: dict, **updates: Any) -> dict:
    cleaned = dict(result)
    cleaned.update(updates)
    for key in ("cookies", "browserStorage", "browser_storage", "data"):
        cleaned.pop(key, None)
    return cleaned


def persist_cookies_result(*, user_id: int, ai_config_id: Optional[int], result: Any) -> Any:
    if not isinstance(result, dict) or not user_id:
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    cookies = result.get("cookies") or data.get("cookies")
    browser_storage = result.get("browserStorage") or result.get("browser_storage")
    if not (cookies or data.get("browserStorage")):
        return result
    try:
        workspace_dir = _workspace_dir(int(user_id), ai_config_id)
        cookies_dir = os.path.join(workspace_dir, "cookies")
        os.makedirs(cookies_dir, exist_ok=True)
        account = str(data.get("account") or "").strip() or "unknown"
        filename = f"cookies_{re.sub(r'[^a-zA-Z0-9_-]+', '_', account)[:30]}_{int(time.time() * 1000)}.json"
        absolute_path = os.path.abspath(os.path.join(cookies_dir, filename))
        payload = _cookie_payload(result, data, cookies, browser_storage)
        with open(absolute_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        from api.core.config import user_workspace_dir

        relative_path = os.path.relpath(absolute_path, user_workspace_dir(int(user_id))).replace(os.sep, "/")
        count = len(cookies) if isinstance(cookies, list) else result.get("cookieCount")
        return _clean_result(
            result, saved_to_server=True, server_path=absolute_path,
            workspace_path=relative_path, file_name=filename, cookieCount=count,
        )
    except Exception as exc:
        logger.exception("persist cookie snapshot to AI workspace failed")
        return _clean_result(result, saved_to_server=False, save_error=str(exc))
