# -*- coding: utf-8 -*-
"""Reserved hooks for future library (workshop) MCP tools."""

from pathlib import Path
from typing import Any, Dict

_DIRECTION_FILE = Path(__file__).resolve().parent / "direction.md"

_GUIDED_TOOLS = set()


def load_direction() -> str:
    """读取方向文档；不存在或为空则返回空串。"""
    try:
        text = _DIRECTION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text


def before_execute(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """回调服务端前的入参钩子。默认原样放行。"""
    return args


def after_execute(tool: str, args: Dict[str, Any], result: Any) -> Any:
    """返回给 AI 前的结果钩子。默认给关键工具附带方向指引。"""
    if tool not in _GUIDED_TOOLS:
        return result
    direction = load_direction()
    if not direction:
        return result
    if isinstance(result, dict):
        enriched = dict(result)
        enriched["workshop_direction"] = direction
        return enriched
    return result
