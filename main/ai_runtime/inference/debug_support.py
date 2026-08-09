"""Debug rendering and stable provider-session identifiers."""

import hashlib
import logging
import os
import sys
from typing import Any, Optional
from urllib.parse import urlparse

from api.core.settings import settings


logger = logging.getLogger(__name__)


def ai_debug_enabled() -> bool:
    return bool(settings.ai_debug)


def ai_debug_color_enabled() -> bool:
    if not settings.ai_debug_color or os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def ai_color(text: str, code: str) -> str:
    if not ai_debug_color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def ai_short(value: Any, limit: int = 48) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return f"{text[: max(1, limit - 1)]}…"


def ai_short_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not text:
        return "-"
    if text.startswith("run_") and len(text) > 12:
        return f"run_{text[4:12]}"
    return ai_short(text, 16)


def ai_short_base_url(base_url: str) -> str:
    text = str(base_url or "").strip()
    if not text:
        return "-"
    parsed = urlparse(text)
    if not parsed.netloc:
        return ai_short(text, 48)
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc}{path}" if path else parsed.netloc


def heysure_provider_session_id(
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
) -> str:
    raw = f"{int(user_id)}\0{ai_config_id or 0}\0{ai_kind or ''}\0{session_id or ''}"
    return "heysure-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ai_debug_log(message: str) -> None:
    if ai_debug_enabled():
        logger.debug(message)


def ai_debug_stage(stage: str, message: str, color: str = "36") -> None:
    ai_debug_log(f"{ai_color(stage, color)} {message}")
