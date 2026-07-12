# -*- coding: utf-8 -*-
"""完整图书馆 MCP 目录：任务管理与治理类注册表工具。"""

from typing import Any, Dict


def library_mcp_full_payload(user_id: int = 0) -> Dict[str, Any]:
    """完整图书馆 MCP = 需绑定图书馆的任务管理与治理类注册表工具。"""
    from api.services.knowledge.librarian_service import _intrinsic_properties_payload

    governance = _intrinsic_properties_payload(int(user_id or 0), scope="library")
    return {
        "description": "完整图书馆 MCP（含 task.manage 与治理类工具，知识库操作经 knowledge.manage）。",
        "scope": "library_full",
        "total": int(governance.get("total") or 0),
        "governance": governance,
    }
