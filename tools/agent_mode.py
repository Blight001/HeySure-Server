# -*- coding: utf-8 -*-
"""``mode.manage`` 工具：工作模式的创建 / 修改 / 删除 / 使用（模式清单按 AI 隔离）。

模式 = 一段前置 prompt。AI 在对话前判断当前工作环境，用本工具切换到对应模式；
切换（use）只在工具结果中返回该模式的完整 prompt+说明（不改写系统提示/人格）。
运行时在对话初始会确保模式说明作为上下文消息进入模型（模拟初始 mode.use 结果），
后续仅通过工具结果传递。current_mode_key 主要用于工具门禁（initial 模式收走设备 MCP）。
本工具属于「工具箱」设备（服务端固定工具、非图书馆绑定），因此所有绑定工具箱的
AI 都可调用它来管理与切换自己的模式。默认内置 3 个模式：普通对话 / 任务 / 学习。
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.common.value_utils import to_bool
from api.services.mcp import agent_mode_store as store


def _parse_allow_device_mcp(args: dict) -> Optional[bool]:
    """读取可选的 allow_device_mcp 参数；未传时返回 None（保持不变 / 用默认）。"""
    raw = (args or {}).get("allow_device_mcp")
    if raw is None:
        return None
    return to_bool(raw, default=True)


def _mode_list(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    modes = store.list_modes(user_id, ai_config_id)
    current = ""
    if ai_config_id:
        eff = store.effective_mode_prompt(user_id, ai_config_id)
        current = eff.get("mode_key", "") if eff else ""
    return {
        "modes": [
            {
                "mode_key": m["mode_key"],
                "name": m["name"],
                "description": m["description"],
                "allow_device_mcp": m["allow_device_mcp"],
                "is_builtin": m["is_builtin"],
            }
            for m in modes
        ],
        "current_mode_key": current,
        "note": (
            "用 action=get 读取某模式完整 prompt；action=use 切换当前 AI 的模式。"
            "allow_device_mcp 是模式类型：false 的模式下无法调用设备端（桌面/浏览器/安卓）MCP。"
        ),
    }


def _mode_get(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for get")
    store.ensure_builtin_modes(user_id, ai_config_id)
    mode = store.get_mode(user_id, key, ai_config_id)
    if not mode:
        raise HTTPException(status_code=404, detail=f"mode not found: {key}")
    return mode


def _mode_create(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    return store.create_mode(
        user_id,
        name=str(args.get("name") or ""),
        prompt=str(args.get("prompt") or args.get("text") or ""),
        description=str(args.get("description") or ""),
        mode_key=str(args.get("mode_key") or args.get("key") or ""),
        allow_device_mcp=_parse_allow_device_mcp(args),
        ai_config_id=ai_config_id,
    )


def _mode_update(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for update")
    prompt_arg = args.get("prompt", args.get("text"))
    return store.update_mode(
        user_id,
        key,
        name=args.get("name"),
        prompt=prompt_arg,
        description=args.get("description"),
        allow_device_mcp=_parse_allow_device_mcp(args),
        ai_config_id=ai_config_id,
    )


def _mode_delete(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for delete")
    return store.delete_mode(user_id, key, ai_config_id)


def _mode_use(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for use")

    target_id = args.get("target_ai_config_id", ai_config_id)
    if not target_id:
        raise HTTPException(
            status_code=400,
            detail="无法确定要切换模式的 AI（缺少运行上下文 ai_config_id）",
        )
    target_id = int(target_id)

    # 默认只允许切换自己的模式；切换别的 AI 需要管理权限。
    if ai_config_id is not None and target_id != int(ai_config_id):
        from sqlmodel import Session, select
        from api.database import engine
        from api.models import AssistantAIConfig
        from api.services.access.governance import assert_can_manage_or_legacy

        with Session(engine) as session:
            caller = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.id == int(ai_config_id),
                )
            ).first()
            target = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.id == target_id,
                )
            ).first()
            if not caller or not target:
                raise HTTPException(status_code=404, detail="AI config not found")
            denial = assert_can_manage_or_legacy(session, user_id, caller, target)
            if denial:
                raise HTTPException(status_code=403, detail=denial)

    result = store.set_current_mode(user_id, target_id, key)
    mode = result.get("mode") or {}
    allow_device = bool(mode.get("allow_device_mcp", True))
    # 只在工具结果里返回该模式的说明（description + prompt），由模型据此在对话中调整行为。
    # 不改写人格 / 系统 prompt；current_mode_key 仅作当前模式的记录，供 UI 展示与查询。
    payload: Dict[str, Any] = {
        "success": True,
        "ai_config_id": result.get("ai_config_id"),
        "current_mode_key": result.get("current_mode_key"),
        "name": mode.get("name"),
        "description": mode.get("description"),
        "prompt": mode.get("prompt"),
        "allow_device_mcp": allow_device,
        "note": (
            "已切换工作模式。以下 prompt 是该模式的说明，请在本次对话中据此调整行为；"
            "它不会改写你的人格 / 系统提示，仅作为本轮起对话的行动指引。"
        ),
    }
    # 切到允许调用设备端 MCP 的模式时，说明结尾自动附带当前可用的设备端 MCP 目录
    # （只列在工坊 / 设备权限中勾选放行且设备在线的工具）。
    if allow_device:
        catalog = _device_mcp_catalog_text(user_id, target_id)
        if catalog:
            payload["device_mcp_catalog"] = catalog
            payload["note"] += (
                "本模式允许调用设备端 MCP：device_mcp_catalog 列出了当前可用的设备端工具"
                "（名称: 简介），调用前先用 mcp.describe+tool 获取参数 schema。"
            )
        else:
            payload["note"] += (
                "本模式允许调用设备端 MCP，但当前没有可用的设备端工具"
                "（设备离线，或未在工坊 / 设备权限中勾选允许调用）。"
            )
    else:
        payload["note"] += (
            "本模式不允许调用设备端（桌面 / 浏览器 / 安卓）MCP；"
            "如需操作设备，请先切换到允许设备端 MCP 的工作模式。"
        )
    return payload


def _device_mcp_catalog_text(user_id: int, ai_config_id: int) -> str:
    """当前 AI 可用的设备端 MCP 工具目录（每行「- 名称: 简介」）。

    范围与运行时门禁一致：按设备权限 / 工坊勾选 + 在线设备解析（DB 为准），
    不含图书馆 workshop 工具。任何异常都静默返回空串，不影响模式切换本身。
    """
    try:
        from connector_runtime.dispatch.desktop_device_tools import (
            endpoint_tools_for_config,
            is_workshop_tool,
        )
        from api.devices.presence import online_tool_defs_for_user

        names = sorted(
            name for name in endpoint_tools_for_config(ai_config_id, user_id)
            if not is_workshop_tool(name)
        )
        if not names:
            return ""
        defs = online_tool_defs_for_user(user_id)
        lines = []
        for name in names:
            spec = defs.get(name) or {}
            desc = str(spec.get("description") or "").strip()
            first_line = desc.splitlines()[0].strip() if desc else ""
            if len(first_line) > 90:
                first_line = first_line[:90].rstrip() + "…"
            lines.append(f"- {name}: {first_line}" if first_line else f"- {name}")
        return "\n".join(lines)
    except Exception:
        return ""


_MODE_ACTIONS = {
    "list": _mode_list,
    "get": _mode_get,
    "create": _mode_create,
    "update": _mode_update,
    "delete": _mode_delete,
    "use": _mode_use,
}

_MODE_ACTION_ALIASES = {
    "read": "get",
    "modify": "update",
    "edit": "update",
    "remove": "delete",
    "switch": "use",
    "set": "use",
}


def _mode_manage(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    """统一工作模式工具：按 ``action`` 分发到具体处理器。"""
    raw = str((args or {}).get("action") or "").strip().lower()
    action = _MODE_ACTION_ALIASES.get(raw, raw)
    if not action:
        raise HTTPException(status_code=400, detail="action is required for mode.manage")
    handler = _MODE_ACTIONS.get(action)
    if handler is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. 可用: {', '.join(sorted(_MODE_ACTIONS))}",
        )
    return handler(user_id, args or {}, ai_config_id)


MODE_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(_MODE_ACTIONS),
            "description": (
                "操作类型："
                "list 列出全部模式与当前模式；"
                "get 读取某模式完整 prompt（需 mode_key）；"
                "create 创建自定义模式（需 name+prompt）；"
                "update 修改模式（需 mode_key，可改 name/description/prompt；内置模式也可改 prompt）；"
                "delete 删除自定义模式（需 mode_key；内置模式不可删）；"
                "use 把当前 AI 切换到某模式（需 mode_key），返回该模式说明供你据此调整行为（不改写系统提示）。"
            ),
        },
        "mode_key": {
            "type": "string",
            "description": (
                "模式标识。get/update/delete/use 必填；create 可选（省略则按 name 自动生成）。"
                "内置：initial=初始对话(默认，只聊天、无设备工具) / task=任务 / learning=学习。"
            ),
        },
        "name": {"type": "string", "description": "create 必填 / update 可选：模式显示名。"},
        "description": {"type": "string", "description": "create/update 可选：一句话说明该模式适用场景。"},
        "allow_device_mcp": {
            "type": "boolean",
            "description": (
                "create/update 可选：模式类型——是否允许调用设备端（桌面/浏览器/安卓）MCP。"
                "false 的模式下设备端工具会被收走且用户无法在对话中勾选附带；"
                "create 省略默认 true。内置 initial 为 false，task/learning 为 true。"
            ),
        },
        "prompt": {
            "type": "string",
            "description": "create 必填 / update 可选：切换到该模式时注入的前置 prompt 正文（可多行）。",
        },
        "target_ai_config_id": {
            "type": "integer",
            "description": "use 可选：要切换模式的目标 AI；省略则切换当前 AI 自己。切换他人需管理权限。",
        },
    },
    "required": ["action"],
}
