"""Classify MCP tools whose results contain screenshot images.

Endpoint devices may namespace their dynamic tools (for example,
``aifree.browser+screenshot``).  Media handling must classify those names by
capability instead of relying on an exact built-in tool name.
"""

import re


_SCREENSHOT_TOOL_KEYS = (
    "screen_capture_region",
    "vision_capture_mouse",
    "browser_screenshot",
    "screen_capture",
    "vision_capture",
)


def _tool_name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def canonical_screenshot_tool_name(name: str, *, include_mouse_click: bool = False) -> str:
    """Return the screenshot capability represented by a possibly namespaced name.

    All punctuation is treated as a separator so both the canonical AI-FREE
    spelling (``aifree.browser+screenshot``) and its legacy underscore spelling
    resolve to ``browser_screenshot``.
    """
    key = _tool_name_key(name)
    candidates = (*_SCREENSHOT_TOOL_KEYS, "mouse_click") if include_mouse_click else _SCREENSHOT_TOOL_KEYS
    for candidate in candidates:
        if key == candidate or key.endswith(f"_{candidate}"):
            return candidate
    return ""


def is_screenshot_tool_name(name: str, *, include_mouse_click: bool = False) -> bool:
    return bool(canonical_screenshot_tool_name(name, include_mouse_click=include_mouse_click))
