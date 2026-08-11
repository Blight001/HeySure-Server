"""游戏世界（Agent 进化与实战区域）接口。

只承载**表现层元数据**（皮肤等）；一切业务操作（启停 / 派任务 / 绑定设备 /
知识审批）仍走各自现有路由，世界页只是调用方（设计方案 §0 设计原则①）。
"""

import json
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from api.database import get_session
from api.models import AssistantAIConfig, WorldActorMeta
from .auth import get_current_user


PREFIX = "/api/world"
router = APIRouter()

# 外观元数据默认值；skin_json 里只存与默认不同的键
APPEARANCE_DEFAULTS = {"skin": "", "tint": "", "scale": 1.0, "aura": ""}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SCALE_MIN, SCALE_MAX = 0.7, 1.4


class ActorMetaUpdate(BaseModel):
    """部分更新：None = 不改该字段（旧客户端只传 skin 时不丢调色等配置）。"""

    skin: Optional[str] = None
    tint: Optional[str] = None
    scale: Optional[float] = None
    aura: Optional[str] = None


class DeviceOrderUpdate(BaseModel):
    device_ids: list[str]


def _parse_meta(skin_json: str) -> dict:
    """skin_json → 外观字典（带默认值兜底，容忍历史脏数据）。"""
    try:
        raw = json.loads(skin_json or "{}")
        if not isinstance(raw, dict):
            raw = {}
    except (ValueError, TypeError):
        raw = {}
    meta = dict(APPEARANCE_DEFAULTS)
    skin = str(raw.get("skin") or "")
    if skin:
        meta["skin"] = skin
    tint = str(raw.get("tint") or "")
    if HEX_COLOR_RE.match(tint):
        meta["tint"] = tint
    aura = str(raw.get("aura") or "")
    if HEX_COLOR_RE.match(aura):
        meta["aura"] = aura
    try:
        scale = float(raw.get("scale", 1.0))
    except (ValueError, TypeError):
        scale = 1.0
    if SCALE_MIN <= scale <= SCALE_MAX:
        meta["scale"] = scale
    return meta


def _meta_item(row: WorldActorMeta) -> dict:
    return {"ai_config_id": row.ai_config_id, **_parse_meta(row.skin_json)}


@router.get("/snapshot")
async def world_snapshot(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """世界页首屏聚合：一次请求替代 6 个并行请求。

    复用各域现有 handler / service（不复制投影逻辑）；任何子项失败都
    降级为空值，不让单个域拖垮整个世界页。
    """
    user = get_current_user(authorization, session)

    from api.devices.world_order import list_world_device_order
    from api.services.world_snapshot import (
        append_bound_offline_agents,
        load_knowledge_snapshot,
        load_pending_proposals,
    )
    from .ai_misc_routes import list_ai_cards
    from .devices import list_connected_devices

    try:
        cards = await list_ai_cards(session=session, authorization=authorization)
    except Exception:
        cards = []
    try:
        connected = list_connected_devices(session=session, authorization=authorization)
        agents = connected.get("agents", [])
    except Exception:
        agents = []
    append_bound_offline_agents(session, user.id, agents)
    knowledge_items, knowledge_active = load_knowledge_snapshot(user.id)
    proposals = load_pending_proposals(user.id)
    meta_rows = session.exec(
        select(WorldActorMeta).where(WorldActorMeta.user_id == user.id)
    ).all()
    actor_meta = [_meta_item(row) for row in meta_rows]
    device_order = list_world_device_order(session, user.id)

    return {
        "cards": cards,
        "agents": agents,
        "knowledge_active": knowledge_active,
        "knowledge_items": knowledge_items,
        "proposals": proposals,
        "actor_meta": actor_meta,
        "device_order": device_order,
    }


@router.get("/devices/order")
def get_device_order(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    from api.devices.world_order import list_world_device_order

    user = get_current_user(authorization, session)
    return {"device_ids": list_world_device_order(session, user.id)}


@router.put("/devices/order")
def update_device_order(
    body: DeviceOrderUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    from api.devices.world_order import save_world_device_order

    user = get_current_user(authorization, session)
    try:
        device_ids = save_world_device_order(session, user.id, body.device_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "device_ids": device_ids}


@router.get("/actors/meta")
def list_actor_meta(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(
        select(WorldActorMeta).where(WorldActorMeta.user_id == user.id)
    ).all()
    return {"items": [_meta_item(row) for row in rows]}


@router.put("/actors/{ai_config_id}/meta")
def update_actor_meta(
    ai_config_id: int,
    body: ActorMetaUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == ai_config_id,
            AssistantAIConfig.user_id == user.id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI 成员不存在")
    row = session.exec(
        select(WorldActorMeta).where(
            WorldActorMeta.user_id == user.id,
            WorldActorMeta.ai_config_id == ai_config_id,
        )
    ).first()
    if not row:
        row = WorldActorMeta(user_id=user.id, ai_config_id=ai_config_id)
    meta = _parse_meta(row.skin_json)

    if body.skin is not None:
        skin = str(body.skin).strip()
        # 皮肤 key 是前端资产文件名；只做基本防注入校验，不维护白名单
        # （资产由前端 manifest 管理，后端不感知具体皮肤列表）。
        if len(skin) > 64 or any(c in skin for c in "/\\<>\"'"):
            raise HTTPException(status_code=400, detail="非法皮肤标识")
        meta["skin"] = skin
    if body.tint is not None:
        tint = str(body.tint).strip()
        if tint and not HEX_COLOR_RE.match(tint):
            raise HTTPException(status_code=400, detail="非法调色值（应为 #RRGGBB）")
        meta["tint"] = tint
    if body.aura is not None:
        aura = str(body.aura).strip()
        if aura and not HEX_COLOR_RE.match(aura):
            raise HTTPException(status_code=400, detail="非法光环颜色（应为 #RRGGBB）")
        meta["aura"] = aura
    if body.scale is not None:
        meta["scale"] = round(min(SCALE_MAX, max(SCALE_MIN, float(body.scale))), 2)

    # 只存非默认键，保持 skin_json 紧凑
    stored = {k: v for k, v in meta.items() if v != APPEARANCE_DEFAULTS[k]}
    row.skin_json = json.dumps(stored, ensure_ascii=False)
    row.updated_at = time.time()
    session.add(row)
    session.commit()
    return {"ok": True, "ai_config_id": ai_config_id, **meta}
