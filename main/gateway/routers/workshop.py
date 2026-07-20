"""``/api/workshop`` — 服务端内置图书馆 Agent 的绑定接口。

工坊按账号自动上线（无需用户运行独立程序），本路由只管"哪个 AI 绑定了
工坊"：当前提供传承思想列表、带行号详情、安装、按行编辑和删除 MCP。
工坊与 AI 是 **1:1 绑定**——同一时间只能绑定一个 AI 数字成员；
已被占用的工坊必须先解绑，不能由新成员直接替换。

工具执行不走 REST：调度层（device_dispatch 的 workshop 分支）直接进程内
调用 ``library.engine.execute_tool``，其中完成白名单/归属/绑定复核。
"""

from typing import Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from api.database import get_session
from api.models import AssistantAIConfig
from api.devices.workshop_bindings import (
    bound_config_id_for_agent,
    bound_config_ids_for_agent,
    set_workshop_binding,
    workshop_device_ids_for_config,
)
from .auth import get_current_user

# 自动挂载默认前缀 /api → 实际路径 /api/workshop/*
router = APIRouter(prefix="/workshop", tags=["workshop"])


class WorkshopBindRequest(BaseModel):
    ai_config_id: int
    device_id: str
    bound: bool = True


def _load_owned_config(session: Session, user_id: int, ai_config_id) -> AssistantAIConfig:
    if not ai_config_id:
        raise HTTPException(status_code=400, detail="ai_config_id is required")
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == int(ai_config_id),
            AssistantAIConfig.user_id == user_id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI config not found")
    return cfg


def _config_name(session: Session, user_id: int, ai_config_id: Optional[int]) -> str:
    if not ai_config_id:
        return ""
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == int(ai_config_id),
            AssistantAIConfig.user_id == user_id,
        )
    ).first()
    return str(cfg.name or "").strip() if cfg else f"AI-{ai_config_id}"


@router.get("/bindings")
def list_workshop_bindings(
    ai_config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """列出该用户的工坊（在线状态 + 当前绑定的成员 + 是否绑定到指定 AI）。

    内置工坊自动上线，所以列表至少包含一条常在线条目。"""
    user = get_current_user(authorization, session)
    cfg = _load_owned_config(session, user.id, ai_config_id)

    from api.devices.presence import online_workshop_agents_for_user
    from tools import engine as toolbox_engine
    from library import engine as workshop_engine

    workshop_engine.ensure_presence_for_user(user.id)
    bound_ids = set(workshop_device_ids_for_config(user.id, cfg.id))
    online = {device_id: caps for device_id, caps in online_workshop_agents_for_user(user.id)}

    library_device_id = workshop_engine.device_id_for_user(user.id)
    toolbox_device_id = toolbox_engine.toolbox_device_id_for_user(user.id)
    names: Dict[str, str] = {
        library_device_id: workshop_engine.WORKSHOP_DISPLAY_NAME,
        toolbox_device_id: toolbox_engine.TOOLBOX_DISPLAY_NAME,
    }

    items = []
    # 图书馆与工具箱两个内置作坊始终出现在列表里（工具箱无 presence，靠这里补齐）。
    for device_id in sorted(set(online) | bound_ids | {library_device_id, toolbox_device_id}):
        is_toolbox = device_id == toolbox_device_id
        bound_cfg_id = bound_config_id_for_agent(user.id, device_id)
        if is_toolbox:
            tools = toolbox_engine.toolbox_capability_names()
            online_state = True  # 工具箱内置常在线（无 socket presence）
        else:
            tools = sorted(online.get(device_id) or [])
            online_state = device_id in online
        items.append({
            "device_id": device_id,
            "name": names.get(device_id) or device_id,
            "online": online_state,
            "tools": tools,
            "bound": device_id in bound_ids,
            "bound_ai_config_id": bound_cfg_id,
            "bound_ai_name": _config_name(session, user.id, bound_cfg_id),
            "is_toolbox": is_toolbox,
            "multi": is_toolbox,
        })
    return {"ai_config_id": cfg.id, "agents": items}


@router.post("/bindings")
def update_workshop_binding(
    payload: WorkshopBindRequest,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = _load_owned_config(session, user.id, payload.ai_config_id)
    device_id = str(payload.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    from tools import engine as toolbox_engine

    is_toolbox = device_id == toolbox_engine.toolbox_device_id_for_user(user.id)
    # 图书馆绑定是其中所有 MCP 的唯一权限门槛。
    if (
        not is_toolbox
        and bool(payload.bound)
        and str(cfg.ai_role or "") not in ("digital_member", "assistant_admin")
    ):
        raise HTTPException(status_code=400, detail="图书馆只能绑定数字成员或辅助管理员")
    occupied_by = bound_config_ids_for_agent(user.id, device_id) - {int(cfg.id)}
    if bool(payload.bound) and occupied_by:
        occupied_id = sorted(occupied_by)[0]
        raise HTTPException(
            status_code=409,
            detail=f"该作坊已被 {_config_name(session, user.id, occupied_id)} 绑定，请先解绑",
        )
    stored = set_workshop_binding(
        user.id, device_id, cfg.id, bound=bool(payload.bound), single=True
    )

    # 解绑工具箱时，顺便把这个 AI 配置里残留的老 MCP 名字和 gated 工具清理干净
    if is_toolbox and not payload.bound:
        try:
            from tools.engine import sanitize_mcp_tools
            cleaned = sanitize_mcp_tools(cfg.mcp_tools, user_id=user.id, ai_config_id=cfg.id)
            if cleaned != (cfg.mcp_tools or ""):
                cfg.mcp_tools = cleaned
                session.add(cfg)
                session.commit()
                session.refresh(cfg)
        except Exception:
            pass

    # 绑定/解绑后推送更新 device list，让作坊面板能立即看到 toolbox 的 boundAiConfigIds 变化
    try:
        from api.devices.live import emit_agent_list_for_user
        import asyncio
        asyncio.create_task(emit_agent_list_for_user(user.id))
    except Exception:
        pass

    return {
        "ai_config_id": cfg.id,
        "device_id": device_id,
        "bound": stored,
        "replaced_ai_config_id": None,
        "replaced_ai_name": "",
    }
