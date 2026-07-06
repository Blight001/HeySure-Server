# -*- coding: utf-8 -*-
"""旧版细粒度 MCP 工具名 → 合并后的统一 ``*.manage`` 工具名（运行时归一）。

历史重构把 ``conversation.create`` / ``prompt.list_targets`` / ``admin.get_overview``
等一批细粒度工具合并成 ``*.manage(action=...)``。一次性迁移会重写已落库的
``cfg.mcp_tools`` / 角色授权，但**未迁到或迁后又写入旧名**的配置仍可能存着旧名；
运行时解析 allow-list 时若不归一，这些旧名会以「注册表里已不存在、无描述」的死
工具形式出现在 ``[动态 MCP 说明]`` 目录里。

因此这里把同一张映射表抽成运行时与迁移共用的单一来源：解析 allow-list 时套用本表，
旧名就地映射成现有工具，无需数据迁移即可自愈存量配置。
"""

from typing import Dict, Iterable, Set

# Legacy granular MCP tool name -> unified ``*.manage`` tool. Single source shared
# by the one-time migration (``api.core.migrations``) and runtime allow-list parsing.
LEGACY_TOOL_RENAMES: Dict[str, str] = {
    # 会话
    "conversation.create": "conversation.manage",
    "conversation.delete": "conversation.manage",
    "conversation.list": "conversation.manage",
    "conversation.detail": "conversation.manage",
    "conversation.edit": "conversation.manage",
    "conversation.compress": "conversation.manage",
    "conversation.switch": "conversation.manage",
    "conversation.new": "conversation.manage",
    "conversation.forget_before_current": "conversation.manage",
    "conversation.find": "conversation.manage",
    # 任务管理（完成用 plan.finish 或自然结束，task 域仅管理）
    "task.create": "task.manage",
    "task.list": "task.manage",
    "task.update": "task.manage",
    "task.delete": "task.manage",
    # plan 域：phase 收归 plan 子操作
    "phase.complete": "plan.phase_complete",
    # Prompt
    "prompt.list_targets": "prompt.manage",
    "prompt.read_ai": "prompt.manage",
    "prompt.write_ai": "prompt.manage",
    "prompt.read_system": "prompt.manage",
    "prompt.write_system": "prompt.manage",
    # admin 合并入 admin.manage
    "admin.list_agents": "admin.manage",
    "admin.get_overview": "admin.manage",
}


def normalize_legacy_tool_name(name: str) -> str:
    """把单个旧工具名映射到当前名；非旧名原样返回。"""
    key = str(name or "").strip()
    return LEGACY_TOOL_RENAMES.get(key, key)


def normalize_legacy_tool_names(names: Iterable[str]) -> Set[str]:
    """把一组工具名里的旧名就地归一成当前名（去空、去重）。"""
    out: Set[str] = set()
    for raw in names or ():
        key = str(raw or "").strip()
        if key:
            out.add(LEGACY_TOOL_RENAMES.get(key, key))
    return out


def fully_clean_tool_names(names: Iterable[str]) -> Set[str]:
    """彻底清理工具名列表：归一旧名 + 彻底删除任何残留的老细粒度名字 + 去重。
    确保 prompt 里永远不会再出现 admin.get_overview / prompt.read_ai 这类老名字。
    """
    normalized = normalize_legacy_tool_names(names)
    legacy_old_names = set(LEGACY_TOOL_RENAMES.keys())
    cleaned = {n for n in normalized if n not in legacy_old_names}
    return cleaned
