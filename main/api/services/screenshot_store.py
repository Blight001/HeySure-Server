"""Persist screenshot tool results: decode base64 data URLs and write the image
into the user's workspace ``Screenshots`` folder, optionally exposing a public
temp-image URL."""

import base64
import os
import re
import time
from typing import Any, Dict, Optional

from ..core.config import user_workspace_dir
from ..core.settings import settings
from .temp_image_store import save_temp_image

DATA_URL_RE = re.compile(r"^data:image/(?P<ext>png|jpeg|jpg|webp);base64,(?P<data>.+)$", re.IGNORECASE | re.DOTALL)


def _safe_segment(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "").strip())
    text = text.strip("._")
    return text[:80] or fallback


def _extract_data_url(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("dataUrl", "data_url", "imageDataUrl", "screenshotDataUrl"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith("data:image/"):
                return value
    return ""


def should_persist_screenshot_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("save_to_server") is True or result.get("upload_to_server") is True


def persist_screenshot_result(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    result: Any,
) -> Optional[Dict[str, Any]]:
    if not should_persist_screenshot_result(result):
        return None
    data_url = _extract_data_url(result)
    if not data_url:
        return None
    match = DATA_URL_RE.match(data_url)
    if not match:
        return None
    ext = match.group("ext").lower()
    if ext == "jpeg":
        ext = "jpg"
    raw = base64.b64decode(match.group("data"), validate=False)
    if not raw:
        return None

    root = user_workspace_dir(int(user_id))
    folder = os.path.join(root, "Screenshots")
    os.makedirs(folder, exist_ok=True)
    cfg_part = f"ai{int(ai_config_id)}" if ai_config_id else "ai"
    tool_part = _safe_segment(tool.replace(".", "_"), "screenshot")
    filename = f"{cfg_part}_{tool_part}_{int(time.time() * 1000)}.{ext}"
    abs_path = os.path.abspath(os.path.join(folder, filename))
    with open(abs_path, "wb") as fh:
        fh.write(raw)
    rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
    saved = {
        "server_path": abs_path,
        "workspace_path": rel_path,
        "file_name": filename,
        "bytes": len(raw),
        "media_type": "image",
    }
    if settings.public_base_url:
        try:
            temp_filename, temp_media_type, _ = save_temp_image(raw, ext)
            public_url = f"{settings.public_base_url.rstrip('/')}/tmp-images/{temp_filename}"
            saved["public_url"] = public_url
            saved["image_url"] = public_url
            saved["temp_file_name"] = temp_filename
            saved["temp_media_type"] = temp_media_type
            saved["temp_ttl_seconds"] = int(settings.temp_image_ttl_seconds)
        except Exception:
            pass
    return saved


def attach_persisted_screenshot(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    tool: str,
    result: Any,
) -> Any:
    saved = persist_screenshot_result(
        user_id=user_id,
        ai_config_id=ai_config_id,
        tool=tool,
        result=result,
    )
    if not saved or not isinstance(result, dict):
        return result
    next_result = dict(result)
    next_result["uploaded"] = True
    next_result["server_path"] = saved["server_path"]
    next_result["workspace_path"] = saved["workspace_path"]
    next_result["file_name"] = saved["file_name"]
    next_result["media_type"] = saved["media_type"]
    next_result["bytes"] = saved["bytes"]
    for key in ("public_url", "image_url", "temp_file_name", "temp_media_type", "temp_ttl_seconds"):
        if key in saved:
            next_result[key] = saved[key]
    return next_result
