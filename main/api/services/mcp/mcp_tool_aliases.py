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

import re
from typing import Dict, Iterable, Optional, Set

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
    # 任务管理（计划流统一走 todo.manage，task 域仅管理后台任务）
    "task.create": "task.manage",
    "task.list": "task.manage",
    "task.update": "task.manage",
    "task.delete": "task.manage",
    # 历史计划工具全部收归 todo.manage；handler 会按参数推断旧调用动作。
    "phase.complete": "todo.manage",
    "plan.create": "todo.manage",
    "plan.phase+complete": "todo.manage",
    "plan.finish": "todo.manage",
    # Prompt
    "prompt.list_targets": "prompt.manage",
    "prompt.read_ai": "prompt.manage",
    "prompt.write_ai": "prompt.manage",
    "prompt.read_system": "prompt.manage",
    "prompt.write_system": "prompt.manage",
    # admin 合并入 admin.manage
    "admin.list_agents": "admin.manage",
    "admin.get_overview": "admin.manage",
    # 2026-07 工具名去下划线：名字内部的 _ 改成 +。模型经常把 mcp.xxx 写成
    # mcp_xxx（原生 function 名不允许 .），若名字本身还带下划线就无法唯一还原；
    # 改成 + 后下划线只可能是域分隔符（见 resolve_tool_name）。
    "mcp.describe_tool": "mcp.describe+tool",
    "workspace.run_command": "workspace.run+command",
    "plan.phase_complete": "todo.manage",
    "message.send_to_user": "message.send+to+user",
    "message.send_to_ai": "message.send+to+ai",
    "device_mcp.manage": "device+mcp.manage",
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


def _tool_name_key(name: str) -> str:
    """工具名的宽容匹配键：所有分隔符（. + - _ 等）折叠成单个下划线并小写。

    这样 ``mcp.describe+tool`` / ``mcp_describe_tool`` / ``mcp__describe-tool``
    归一成同一个键 ``mcp_describe_tool``。
    """
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def resolve_tool_name(name: str, candidates: Iterable[str]) -> str:
    """把 AI 发出的工具名宽容解析成 ``candidates`` 里的真实名字。

    模型侧的原生 function 名不允许 ``.``（发给模型时 ``.``→``_``、``+``→``-``），
    模型回吐时经常混用 ``mcp_xxx`` / ``mcp__xxx`` / 旧名，直接精确匹配就会报
    「未知工具」。解析顺序：精确匹配 → 旧名别名 → 分隔符折叠后的唯一匹配。
    无法解析时原样返回（由调用方按未知工具处理）。
    """
    key = str(name or "").strip()
    if not key:
        return key
    cands = {str(c or "").strip() for c in (candidates or ()) if str(c or "").strip()}
    if key in cands:
        return key
    aliased = LEGACY_TOOL_RENAMES.get(key)
    if aliased and aliased in cands:
        return aliased
    norm_map: Dict[str, Optional[str]] = {}
    for cand in cands:
        norm = _tool_name_key(cand)
        # 同键冲突（理论上不该发生）时放弃宽容匹配，只认精确名
        norm_map[norm] = None if norm in norm_map else cand
    # 旧名也参与宽容匹配：mcp_describe_tool 折叠键与旧名 mcp.describe_tool 相同，
    # 其改名目标就是候选里的现名
    for old, new in LEGACY_TOOL_RENAMES.items():
        if new in cands:
            norm = _tool_name_key(old)
            if norm not in norm_map:
                norm_map[norm] = new
    hit = norm_map.get(_tool_name_key(key))
    return hit if hit else key
