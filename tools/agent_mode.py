# -*- coding: utf-8 -*-
"""``mode.manage`` 工具：工作模式的创建 / 修改 / 删除 / 使用。

模式 = 一段前置 prompt。AI 在对话前判断当前工作环境，用本工具切换到对应模式；
运行时会把该模式的 prompt 以 ``[当前工作模式]`` 段注入系统提示，覆盖上一模式。
本工具属于「工具箱」设备（服务端固定工具、非图书馆绑定），因此所有绑定工具箱的
AI 都可调用它来管理与切换自己的模式。默认内置 4 个模式：普通对话 / 任务 / 学习 / 修复。
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.services.mcp import agent_mode_store as store


def _mode_list(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    modes = store.list_modes(user_id)
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
                "is_builtin": m["is_builtin"],
            }
            for m in modes
        ],
        "current_mode_key": current,
        "note": "用 action=get 读取某模式完整 prompt；action=use 切换当前 AI 的模式。",
    }


def _mode_get(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for get")
    store.ensure_builtin_modes(user_id)
    mode = store.get_mode(user_id, key)
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
    )


def _mode_delete(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("mode_key") or args.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required for delete")
    return store.delete_mode(user_id, key)


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
    # 返回完整 prompt，让模型在本轮即可按新模式行动；同时它已持久化，后续轮由运行时注入。
    return {
        "success": True,
        "ai_config_id": result.get("ai_config_id"),
        "current_mode_key": result.get("current_mode_key"),
        "name": mode.get("name"),
        "prompt": mode.get("prompt"),
        "note": "已切换工作模式。该模式 prompt 现已生效，请立即按其要求调整行为。",
    }


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
                "use 把当前 AI 切换到某模式（需 mode_key），切换后该模式 prompt 立即生效。"
            ),
        },
        "mode_key": {
            "type": "string",
            "description": (
                "模式标识。get/update/delete/use 必填；create 可选（省略则按 name 自动生成）。"
                "内置：chat=普通对话 / task=任务 / learning=学习 / fix=修复。"
            ),
        },
        "name": {"type": "string", "description": "create 必填 / update 可选：模式显示名。"},
        "description": {"type": "string", "description": "create/update 可选：一句话说明该模式适用场景。"},
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
