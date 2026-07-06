# -*- coding: utf-8 -*-
"""内置「工具箱」引擎：按绑定门禁放行的服务端固定工具集。

工具箱与图书馆（``library.engine``）是两个并列的服务端内置"虚拟端侧"，但二者
形态不同：

- **图书馆**：1:1 绑定、注册 ``DevicePresence``、自带 handler，工具经工坊分发。
- **工具箱**：多绑（新建 AI 时默认绑定，之后完全由用户在作坊/AI配置中管理绑定与解绑），
  **不注册 presence、不经工坊分发**——它只是一个绑定标记 + 展示条目。
  注意：系统自带 MCP 工具（knowledge.search 等）现已改为直接调用，不再受工具箱绑定硬门禁；
  工具箱绑定/scope 主要用于 UI 分组与治理展示。调用时仍来自常规服务端注册表。

本模块收拢工具箱设备的全部自有逻辑：身份/展示、能力清单、绑定读写、以及"哪些
工具属于工具箱（需绑定）"的门禁判定。中央权限层与注册表核心只调用这里，不再内联
工具箱特例。
"""

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

_TOOLBOX_AGENT_ID_PREFIX = "toolbox_builtin_"
TOOLBOX_DISPLAY_NAME = "工具箱（内置）"
TOOLBOX_PLATFORM = "Workshop-Server"

# 工具箱门禁豁免：自省工具始终可用，避免未绑定时连工具说明都查不了。
TOOLBOX_GATE_EXEMPT: Set[str] = {"mcp.describe_tool"}


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------
def toolbox_device_id_for_user(user_id) -> str:
    return f"{_TOOLBOX_AGENT_ID_PREFIX}{int(user_id)}"


def is_builtin_toolbox_device_id(device_id) -> bool:
    return str(device_id or "").startswith(_TOOLBOX_AGENT_ID_PREFIX)


# ---------------------------------------------------------------------------
# 门禁：哪些服务端固定工具属于「工具箱」（需绑定工具箱才能由 AI 调用）
# ---------------------------------------------------------------------------
def _library_bound_tools() -> Set[str]:
    """图书馆绑定制工具集合（治理类）。工具箱 = 服务端固定工具中除此之外的部分。"""
    try:
        from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

        return set(LIBRARY_BOUND_TOOLS)
    except Exception:
        return set()


def is_toolbox_gated_tool(tool_name: str) -> bool:
    """该工具是否属于「工具箱」分类（用于 UI 分组/展示）。

    注意：运行时调用已不再以此作为硬门禁（系统自带 MCP 直接可用）。仅服务端固定工具
    来自 MCPRegistry；端侧工具走各自 scope，不受此分类影响。"""
    name = str(tool_name or "").strip()
    if not name or name in _library_bound_tools() or name in TOOLBOX_GATE_EXEMPT:
        return False
    try:
        from mcp_runtime.mcp import registry

        return any(str(t.get("name") or "").strip() == name for t in registry.list_tools())
    except Exception:
        # 注册表不可用时 fail-open（不门禁），避免误删任何工具。
        return False


def toolbox_tool_names(all_tool_names: Iterable[str]) -> Set[str]:
    """「工具箱」工具：服务端固定工具中除图书馆绑定工具外的其余部分。"""
    library = _library_bound_tools()
    return {str(name).strip() for name in all_tool_names if str(name).strip() not in library}


def toolbox_capability_names() -> List[str]:
    """工具箱展示用的工具名：服务端固定工具中属于工具箱的部分（非图书馆绑定工具、
    且排除自省工具）。仅供展示，工具箱工具仍由常规服务端注册表提供，不经工坊分发。"""
    try:
        from mcp_runtime.mcp import registry

        names = {str(t.get("name") or "").strip() for t in registry.list_tools() if t.get("name")}
        library = _library_bound_tools()
        return sorted(n for n in names if n and n not in library and n not in TOOLBOX_GATE_EXEMPT)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 绑定（多绑：新建 AI 默认绑定，之后完全听从用户操作）
# ---------------------------------------------------------------------------
def config_bound_to_toolbox(user_id, ai_config_id) -> bool:
    from api.devices.workshop_bindings import config_bound_to_device

    return config_bound_to_device(user_id, ai_config_id, toolbox_device_id_for_user(user_id))


def bind_config_to_toolbox(user_id, ai_config_id) -> bool:
    """把单个 AI 绑定到工具箱（多绑）。用于 AI 创建时默认绑定。"""
    from api.devices.workshop_bindings import set_workshop_binding

    return set_workshop_binding(
        user_id, toolbox_device_id_for_user(user_id), ai_config_id, bound=True, single=False
    )


# ---------------------------------------------------------------------------
# 展示条目
# ---------------------------------------------------------------------------
def toolbox_connected_entry_for_user(user_id) -> Dict[str, Any]:
    """工具箱作坊的虚拟"已连接设备"条目（始终在线，多绑）。

    工具箱不注册 presence、不经工坊分发——它只是一个绑定标记 + 展示条目；工具箱
    工具仍来自常规服务端注册表，由 mcp_runtime 按工具箱绑定逐次校验。
    绑定关系完全由用户通过作坊面板或 AI 配置管理，新建 AI 时会默认调用 bind_config_to_toolbox。"""
    bound_ids: List[int] = []
    try:
        from api.devices.workshop_bindings import bound_config_ids_for_agent

        bound_ids = sorted(bound_config_ids_for_agent(user_id, toolbox_device_id_for_user(user_id)))
    except Exception:
        bound_ids = []
    return {
        "id": toolbox_device_id_for_user(user_id),
        "name": TOOLBOX_DISPLAY_NAME,
        "platform": TOOLBOX_PLATFORM,
        "isWorkshop": True,
        "isToolbox": True,
        "aiConfigId": None,
        "boundAiConfigIds": bound_ids,
        "userId": int(user_id),
        "capabilities": toolbox_capability_names(),
        "version": "builtin",
        "lifecycle": "registered",
        "connectedAt": None,
        "lastSeenAt": time.time(),
        "lastTaskId": None,
        "lastTaskStatus": None,
        "lastTaskAt": None,
        "lastError": None,
        "source": "builtin",
        "dispatchable": False,
    }


def enforce_toolbox_binding(tool_name: str, user_id: int, ai_config_id: Optional[int]) -> None:
    """工具箱绑定门禁：工具箱工具需绑定工具箱。

    没有 ``ai_config_id`` 视为核心 / 管理员直调，放行；自省工具始终放行。非工具箱
    工具（图书馆/端侧/工坊）不在本判定内。拒绝以 ``HTTPException`` 抛出。"""
    if not ai_config_id:
        return
    if not is_toolbox_gated_tool(tool_name):
        return
    if not config_bound_to_toolbox(user_id, ai_config_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail=f"该 AI 未绑定工具箱，无法调用 {tool_name}（请在 AI 配置或世界中绑定工具箱）",
        )


# ---------------------------------------------------------------------------
# 工具箱 MCP scope 默认与 defs（供 DeviceMcpScopeEditor 和默认行为使用）
# ---------------------------------------------------------------------------
def ensure_toolbox_scope_for_user(user_id) -> None:
    """Ensure default full toolbox scope record exists for the user (idempotent best-effort).

    Called during workshop presence ensure so that after binding a new AI, the
    toolbox MCP permission editor has a baseline (user can narrow it).
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    try:
        from api.devices.mcp_permissions import get_scope, set_scope
    except Exception:
        return
    tbid = toolbox_device_id_for_user(uid)
    if get_scope(uid, tbid) is None:
        try:
            caps = set(toolbox_capability_names())
            set_scope(uid, tbid, caps, ai_config_id=None, device_type="toolbox")
        except Exception:
            pass


def toolbox_tool_defs_map() -> Dict[str, Any]:
    """Return tool def map for toolbox capabilities (used by scope editor for schema/desc)."""
    try:
        from mcp_runtime.mcp import registry as mcp_registry
        defs: Dict[str, Any] = {}
        for t in mcp_registry.list_tools():
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            defs[name] = {
                "description": str(t.get("description") or "").strip(),
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
                "destructive": bool(t.get("destructive")),
            }
        return defs
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Toolbox tools for AI config (used by allow-list computation in device dispatch,
# chat runtime, etc.). Consolidated here so toolbox device owns the logic.
# ---------------------------------------------------------------------------
def _parse_int(value: Any) -> Optional[int]:
    try:
        iv = int(value)
        return iv if iv > 0 else None
    except Exception:
        return None


def toolbox_tools_for_config(ai_config_id: Optional[int], user_id: Optional[int] = None) -> Set[str]:
    """Server (non-library) fixed MCP tools for an AI via toolbox binding + scope.

    Used primarily for UI grouping ("工具箱 MCP 权限") and display. Runtime allow-lists
    now force-include system built-ins directly (no hard gate); this may return a subset
    or empty when unbound but calls to knowledge.search etc. will still succeed.
    """
    config_id = _parse_int(ai_config_id)
    uid = _parse_int(user_id)
    if not config_id or not uid:
        return set()
    try:
        from api.devices.mcp_permissions import get_scope
        from mcp_runtime.mcp import registry as mcp_registry
        from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS
    except Exception:
        return set()

    if not config_bound_to_toolbox(uid, config_id):
        return set()

    tbid = toolbox_device_id_for_user(uid)
    scope = get_scope(uid, tbid)

    try:
        names = {str(t.get("name") or "").strip() for t in mcp_registry.list_tools() if t.get("name")}
        tb_all = {n for n in names if n and n not in (LIBRARY_BOUND_TOOLS or set())}
    except Exception:
        tb_all = set()

    if scope is None:
        # No scope saved yet: default to full set after bind (user narrows via 工具箱 MCP 权限)
        return tb_all

    return tb_all & scope


def sanitize_mcp_tools(raw: Optional[str], *, user_id: Optional[int] = None, ai_config_id: Optional[int] = None) -> str:
    """彻底清理一份 mcp_tools 字符串：
    - 归一并删除所有老细粒度名字（admin.get_overview 等）
    - 如果传了 user_id + ai_config_id，会根据当前绑定状态过滤掉需要图书馆的治理工具（系统自带 MCP 如 knowledge.search 现在直接可用，不再因工具箱绑定被清理）
    - 保留自省工具
    返回可直接存回 AssistantAIConfig.mcp_tools 的 JSON 字符串。
    """
    from api.mcp_tool_aliases import fully_clean_tool_names
    from api.services.tasks.task_system import with_workspace_read_by_name_compat
    from connector_runtime.dispatch.desktop_device_tools import strip_endpoint_tool_config_names
    from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
    import json

    try:
        parsed = json.loads(raw or "[]")
        if not isinstance(parsed, list):
            parsed = []
        names = {str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()}

        # 1. 彻底消灭老名字
        names = fully_clean_tool_names(names)

        # 2. 基础处理
        names = strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(names))
        names.update(MCP_INTROSPECTION_TOOLS)

        # 3. 仅清理图书馆绑定类（系统自带工具如 knowledge.search / workspace.* / plan.* 已改为直接调用，不再按工具箱绑定剥离）
        if user_id and ai_config_id:
            try:
                from api.devices.workshop_bindings import config_bound_to_library
                from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

                protected = set(MCP_INTROSPECTION_TOOLS or set())

                if not config_bound_to_library(int(user_id), int(ai_config_id)):
                    names -= (set(LIBRARY_BOUND_TOOLS) - protected)

                # toolbox gated strip removed: system MCPs are direct
            except Exception:
                pass  # fail soft

        cleaned_list = sorted(n for n in names if n)
        return json.dumps(cleaned_list, ensure_ascii=False)
    except Exception:
        # 最坏情况返回只保留自省工具的干净列表
        return json.dumps(sorted(MCP_INTROSPECTION_TOOLS), ensure_ascii=False)
