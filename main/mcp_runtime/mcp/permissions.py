"""Role-based MCP permission scoping.

Defines the default MCP tool set per AI role tier and resolves the effective
allow-set for an AI config. The human admin (主脑) may configure a per-role
allow-list in system settings; an individual member's MCP config can then only
narrow within the set permitted for its role.

Role tiers, from lowest to highest authority:
    digital_member_member   数字成员·普通成员
    digital_member_manager  数字成员·管理者
    assistant_admin         辅助管理员
    admin                   管理员（主脑 / 用户本人）
"""

import json
from typing import Dict, Iterable, List, Optional, Set

ROLE_MEMBER = "digital_member_member"
ROLE_MANAGER = "digital_member_manager"
ROLE_ASSISTANT_ADMIN = "assistant_admin"
ROLE_ADMIN = "admin"

# Lowest authority first.
ROLE_ORDER: List[str] = [ROLE_MEMBER, ROLE_MANAGER, ROLE_ASSISTANT_ADMIN, ROLE_ADMIN]
ROLE_RANK: Dict[str, int] = {role: index for index, role in enumerate(ROLE_ORDER)}

ROLE_LABELS_ZH: Dict[str, str] = {
    ROLE_MEMBER: "数字成员·普通成员",
    ROLE_MANAGER: "数字成员·管理者",
    ROLE_ASSISTANT_ADMIN: "辅助管理员",
    ROLE_ADMIN: "管理员（主脑）",
}

# Roles the admin can configure permissions for in settings (admin/主脑 always
# has every tool, so it is not listed here).
CONFIGURABLE_ROLES: List[str] = [ROLE_ASSISTANT_ADMIN, ROLE_MANAGER, ROLE_MEMBER]

DEFAULT_MIN_ROLE = ROLE_MEMBER

# Minimum role tier required to ever use each MCP tool. Tools absent from this
# map default to DEFAULT_MIN_ROLE (available to everyone). Sensitive tools are
# raised so they can only be granted to the appropriate tiers.
MCP_TOOL_MIN_ROLE: Dict[str, str] = {
    # MCP self-inspection — available to every tier and forced into runtime allow-lists.
    "mcp.describe+tool": ROLE_MEMBER,
    # Web search — external read-only lookup, available to every tier by default.
    "workspace.search": ROLE_MEMBER,
    # Shell command execution is allowed for every member inside the workspace
    # resolved for that AI. Regular members cannot choose an outside cwd.
    "workspace.run+command": ROLE_MEMBER,
    # Unified task management tool (create/list/update/delete). Member floor so
    # everyone can list; create/update/delete are gated to manager+ inside the
    # handler. The self-execution operators below stay separate because the
    # task-runtime flow drives them by name.
    "task.manage": ROLE_MEMBER,
    # 统一计划管理：create/get/edit/delete 全部走 todo.manage；edit 完成当前阶段，
    # 最后阶段更新后由系统自动收尾。
    "todo.manage": ROLE_MEMBER,
    # Unified prompt tool. Member floor so everyone can read its own prompt; the
    # write/system actions are gated inside the handler (write_ai=manager+,
    # read_system=manager+, write_system=assistant_admin+).
    "prompt.manage": ROLE_MEMBER,
    # Unified knowledge-base tool. Member floor; per-action gates live in the
    # handler (create/edit/delete/install=manager+, update_skills/system=assistant_admin+).
    "knowledge.manage": ROLE_MEMBER,
    # Read-only semantic recall for the knowledge base.
    "knowledge.search": ROLE_MEMBER,
    # Send message — outbound to the human user; every tier by default.
    "message.send+to+user": ROLE_MEMBER,
    # Unified conversation tool (list/detail/create/delete/rename/clear/compress/
    # switch/new) — every tier can manage its own scoped sessions.
    "conversation.manage": ROLE_MEMBER,
    # Admin / governance — assistant_admin only.
    "admin.manage": ROLE_ASSISTANT_ADMIN,
    # Self-iterate device MCP tools — writes code that runs on devices with
    # native access, so it sits at the assistant_admin tier.
    "device+mcp.manage": ROLE_ASSISTANT_ADMIN,
}


# 图书馆（绑定制）工具：在角色门槛之外，**还要求调用 AI 已绑定知识工坊（图书馆）**。
# 这些是治理/管理类能力（任务管理、prompt 管理、管理员操作、设备管理、知识库管理）。其余
# 服务端固定工具属于「工具箱」，每个 AI 默认即可用、无需绑定。``knowledge.manage``
# 的门面早已在 workshop 引擎内校验绑定，这里一并登记以表达完整的图书馆工具集，
# 中央分发处的绑定校验对它是幂等的；知识库具体操作经 knowledge.manage action 分发。
LIBRARY_BOUND_TOOLS: Set[str] = {
    "task.manage",
    "prompt.manage",
    "admin.manage",
    "device+mcp.manage",
    "knowledge.manage",
}


def requires_library_binding(tool_name: str) -> bool:
    """该工具是否需要「图书馆」绑定才能由 AI 调用。"""
    return str(tool_name or "").strip() in LIBRARY_BOUND_TOOLS


# 基础对话控制工具：与 ``mcp.describe+tool`` 同类，是模式系统赖以运转的底座。
# 每个 AI 无论角色策略如何都必须能切换 / 管理自己的工作模式（``mode.manage``）——
# 否则既没法从初始模式进工作间拿到设备 MCP，用户也无法在对话「+」面板里切换模式
# （前端切换走同一条 /api/mcp/call）。因此这些工具始终放行，不受「按档位的工具箱
# 白名单」收敛（与 ``mcp.describe+tool`` 的豁免一致，见 agent_mode_store 的
# CHAT_MODE_TOOL_WHITELIST）。
ALWAYS_ALLOWED_BASIC_TOOLS: Set[str] = {"mcp.describe+tool", "mode.manage"}


# 「工具箱」设备（多绑、按绑定门禁放行的服务端固定工具集）整体迁出到独立模块
# ``tools.engine``：门禁判定（is_toolbox_gated_tool / toolbox_tool_names /
# TOOLBOX_GATE_EXEMPT）与绑定逻辑都收在那里，本权限层不再内联工具箱特例。


def tool_min_role(tool_name: str) -> str:
    return MCP_TOOL_MIN_ROLE.get(tool_name, DEFAULT_MIN_ROLE)


def enforce_min_role(user_id: int, ai_config_id: Optional[int], min_role: str) -> None:
    """Raise 403 if the calling AI config's role tier is below ``min_role``.

    Unified ``*.manage`` tools fold several legacy tools (with different minimum
    role tiers) behind one tool name, so the per-tool allow-list can no longer
    express the finer per-action gating. Handlers call this to keep the original
    granularity (e.g. ``task.manage`` allows ``list`` for everyone but reserves
    ``create``/``update``/``delete`` for managers). A missing ``ai_config_id``
    means an admin/core direct call and bypasses the check; an unknown config is
    treated as the lowest tier so writes stay blocked.
    """
    if not ai_config_id:
        return
    from fastapi import HTTPException
    from sqlmodel import Session, select

    from api.database import engine
    from api.models import AssistantAIConfig

    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
    tier = config_role_tier(cfg) if cfg else ROLE_MEMBER
    if ROLE_RANK.get(tier, 0) < ROLE_RANK.get(min_role, 0):
        raise HTTPException(
            status_code=403,
            detail=(
                f"角色「{ROLE_LABELS_ZH.get(tier, tier)}」无权执行该操作"
                f"（需「{ROLE_LABELS_ZH.get(min_role, min_role)}」及以上）"
            ),
        )


def all_registry_tool_names() -> Set[str]:
    from .registry import registry

    return {
        str(tool.get("name") or "").strip()
        for tool in registry.list_tools()
        if tool.get("name")
    }


def config_role_tier(cfg) -> str:
    """Map an AssistantAIConfig to its permission tier."""
    role = str(getattr(cfg, "ai_role", "") or "").strip()
    if role == "assistant_admin":
        return ROLE_ASSISTANT_ADMIN
    member_role = str(getattr(cfg, "digital_member_role", "") or "").strip()
    if member_role == "manager":
        return ROLE_MANAGER
    return ROLE_MEMBER


def role_ceiling_tools(tier: str, all_tool_names: Iterable[str]) -> Set[str]:
    """Configurable tools for a role. Admin settings may show every known tool."""
    return set(all_tool_names)


def role_default_tools(tier: str, all_tool_names: Iterable[str]) -> Set[str]:
    """Default checked tools: all tools whose default minimum tier is at or below ``tier``."""
    rank = ROLE_RANK.get(tier, 0)
    return {
        name
        for name in all_tool_names
        if ROLE_RANK.get(tool_min_role(name), 0) <= rank
    }


def parse_role_permissions(user) -> Dict[str, List[str]]:
    """Parse the admin-configured per-role allow-list stored on the user."""
    raw = getattr(user, "role_mcp_permissions", "") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    from api.services.mcp.mcp_tool_aliases import normalize_legacy_tool_name

    out: Dict[str, List[str]] = {}
    for role, tools in data.items():
        if role in ROLE_RANK and isinstance(tools, list):
            # 存量授权可能还是旧工具名（如 message.send_to_ai），就地归一成现名，
            # 否则改名后的工具会被角色策略误拦。
            out[role] = [
                normalize_legacy_tool_name(str(item).strip())
                for item in tools
                if isinstance(item, str) and str(item).strip()
            ]
    return out


def effective_allowed_for_tier(user, tier: str, all_tool_names: Iterable[str]) -> Set[str]:
    """Tools a given role tier may use.

    If the admin has saved an explicit per-role policy it is honoured (the role
    system stays in force). Otherwise every known tool is allowed — the curated
    ``role_default_tools`` set is only a *default checked* hint for the UI, not a
    hard ceiling, so an operator can grant any MCP tool to any AI from its own
    config without first widening the role policy in System Settings."""
    names = set(all_tool_names)
    ceiling = role_ceiling_tools(tier, names)
    policy = parse_role_permissions(user)
    if tier in policy:
        allowed = {tool for tool in policy[tier] if tool in ceiling}
        # 图书馆治理类工具、自省工具与基础模式控制工具不受「按档位的工具箱白名单」
        # 约束：图书馆工具由绑定门槛 + enforce_min_role 管控；基础对话控制工具
        # （mcp.describe+tool / mode.manage）是系统底座，始终放行，避免运行时天花板把
        # 它们误挡掉——与 clamp_tools_json 的同类豁免保持一致。
        allowed |= (LIBRARY_BOUND_TOOLS | ALWAYS_ALLOWED_BASIC_TOOLS) & ceiling
        return allowed
    return ceiling


def effective_allowed_for_config(user, cfg, all_tool_names: Optional[Iterable[str]] = None) -> Set[str]:
    names = set(all_tool_names) if all_tool_names is not None else all_registry_tool_names()
    return effective_allowed_for_tier(user, config_role_tier(cfg), names)


def clamp_tools_json(user, tier: str, mcp_tools_json: Optional[str]) -> str:
    """Narrow a stored mcp_tools JSON array to what ``tier`` is allowed to use."""
    from connector_runtime.dispatch.desktop_device_tools import is_endpoint_tool_config_name

    names = all_registry_tool_names()
    allowed = effective_allowed_for_tier(user, tier, names)
    try:
        parsed = json.loads(mcp_tools_json or "[]")
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    requested = [
        str(item).strip()
        for item in parsed
        if isinstance(item, str) and str(item).strip()
    ]
    clamped: List[str] = []
    seen: Set[str] = set()
    for tool in requested:
        if tool.startswith("workspace.") and tool not in {
            "workspace.search",
            "workspace.run+command",
        }:
            continue
        # Endpoint desktop/browser tools are governed exclusively by
        # AgentMcpPermission, not by AssistantAIConfig.mcp_tools.
        if is_endpoint_tool_config_name(tool):
            continue
        # 图书馆治理类工具在作坊 UI 按 AI 显式勾选；保留写入 mcp_tools，运行时仍由
        # 绑定门槛 + enforce_min_role 约束，避免角色策略白名单把 prompt 目录里的
        # manage 工具误剥掉。
        if tool in LIBRARY_BOUND_TOOLS:
            if tool not in seen:
                clamped.append(tool)
                seen.add(tool)
            continue
        # Unknown non-endpoint tools are governed elsewhere; keep them as-is.
        if tool not in names or tool in allowed:
            if tool not in seen:
                clamped.append(tool)
                seen.add(tool)
    return json.dumps(clamped, ensure_ascii=False)


def default_role_permissions(all_tool_names: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """Default per-role allow-lists, for settings UI checked state."""
    names = set(all_tool_names) if all_tool_names is not None else all_registry_tool_names()
    return {
        role: sorted(role_default_tools(role, names))
        for role in CONFIGURABLE_ROLES
    }


def role_tool_options(all_tool_names: Optional[Iterable[str]] = None) -> Dict[str, List[str]]:
    """Per-role configurable tool options, for settings UI display."""
    names = set(all_tool_names) if all_tool_names is not None else all_registry_tool_names()
    return {
        role: sorted(role_ceiling_tools(role, names))
        for role in CONFIGURABLE_ROLES
    }
