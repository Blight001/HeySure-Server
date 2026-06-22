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
from api.models import AssistantAIConfig, DevicePresence, WorldActorMeta
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

    from api.services import librarian_service
    from .ai_misc_routes import list_ai_cards
    from .devices import list_connected_devices

    try:
        cards = await list_ai_cards(session=session, authorization=authorization)
    except Exception:
        cards = []
    try:
        connected = await list_connected_devices(session=session, authorization=authorization)
        agents = connected.get("agents", [])
    except Exception:
        agents = []
    # 世界需要保留已绑定但离线的作坊，才能把成员自动安置到出生地。
    # 普通“已连接设备”列表仍只展示在线设备，不受这里的世界投影影响。
    try:
        online_ids = {str(row.get("id") or row.get("deviceId") or "") for row in agents}
        offline_rows = session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == user.id,
                DevicePresence.online == False,  # noqa: E712
                DevicePresence.ai_config_id.is_not(None),
            )
        ).all()
        for row in offline_rows:
            device_id = str(row.device_id or "").strip()
            if not device_id or device_id in online_ids:
                continue
            device_type = str(row.device_type or "").strip()
            agents.append({
                "id": device_id,
                "name": device_id,
                "platform": device_type,
                "isWindowsDesktop": device_type == "desktop",
                "isBrowserExtension": device_type == "browser",
                "isAndroid": device_type == "android",
                "aiConfigId": row.ai_config_id,
                "capabilities": [],
                "lifecycle": "offline",
                "online": False,
                "lastError": None,
            })
    except Exception:
        pass
    try:
        knowledge_topics = librarian_service.list_topics(user_id=user.id, status="active")
        knowledge_items = []
        for item in knowledge_topics:
            memory_id = str(item.get("memory_id") or "")
            if not memory_id:
                continue
            # 内置类目（固有技能/固有人格/固有思路/传承技能/传承思想）的正文计算昂贵：
            # 会枚举整个 MCP 注册表并落盘、读取全部人格与系统提示词文件。世界页只用
            # 计数与摘要，完整详情在主控制台知识库按需读取；这里只放轻量元数据，避免
            # 首屏与每次 8s 轮询都重算全部资源。
            if memory_id.startswith("builtin."):
                knowledge_items.append(item)
                continue
            try:
                knowledge_items.append(librarian_service.read(user_id=user.id, memory_id=memory_id))
            except Exception:
                knowledge_items.append(item)
        knowledge_active = len(knowledge_items)
    except Exception:
        knowledge_items = []
        knowledge_active = 0
    try:
        proposals = librarian_service.list_pending_for_review(user_id=user.id)
    except Exception:
        proposals = []
    meta_rows = session.exec(
        select(WorldActorMeta).where(WorldActorMeta.user_id == user.id)
    ).all()
    actor_meta = [_meta_item(row) for row in meta_rows]

    return {
        "cards": cards,
        "agents": agents,
        "knowledge_active": knowledge_active,
        "knowledge_items": knowledge_items,
        "proposals": proposals,
        "actor_meta": actor_meta,
    }


@router.get("/actors/meta")
async def list_actor_meta(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(
        select(WorldActorMeta).where(WorldActorMeta.user_id == user.id)
    ).all()
    return {"items": [_meta_item(row) for row in rows]}


@router.put("/actors/{ai_config_id}/meta")
async def update_actor_meta(
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
