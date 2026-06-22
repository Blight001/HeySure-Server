"""Shared base for the ``/api/ai`` router family: defines the ``APIRouter`` and
shared helpers (prompt section stripping, default ``system_auto_control`` blobs,
role normalization, task-owner resolution) used by the ai_* route modules."""

IS_ROUTER_ENTRY = False

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from sqlmodel import Session, select

from api.models import (
    AssistantAIConfig,
    User,
)

router = APIRouter()
PREFIX = "/api/ai"


def _default_system_auto_control_for_user(user: User) -> str:
    _ = user
    return json.dumps({"enabled": True, "tasks": []}, ensure_ascii=False)

def _normalize_ai_role(value: Optional[str]) -> str:
    role = (value or "").strip().lower()
    return "assistant_admin" if role == "assistant_admin" else "digital_member"

def _normalize_digital_member_role(value: Optional[str]) -> str:
    role = (value or "").strip().lower()
    return "manager" if role == "manager" else "member"

def _append_task_title_suffix(title: str) -> str:
    clean = title.strip() or "未命名任务"
    # Add time suffix to avoid duplicate names in DB.
    return f"{clean}_{time.strftime('%Y%m%d%H%M%S', time.localtime())}"

def _resolve_task_owner_cfg(
    session: Session,
    user_id: int,
    caller_cfg: AssistantAIConfig,
    payload_body: Dict[str, Any],
) -> AssistantAIConfig:
    caller_role = str(caller_cfg.ai_role or "").strip()
    if caller_role == "digital_member":
        return caller_cfg
    if caller_role != "assistant_admin":
        raise HTTPException(status_code=400, detail="Only digital_member or assistant_admin supports task scheduler")

    raw_target = payload_body.get("target_ai_config_id")
    if raw_target is None:
        raw_target = payload_body.get("target_config_id")
    if raw_target is not None:
        try:
            target_id = int(raw_target)
        except Exception:
            raise HTTPException(status_code=400, detail="target_ai_config_id must be an integer")
        target_cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == target_id,
            )
        ).first()
        if not target_cfg:
            raise HTTPException(status_code=404, detail="Target AI config not found")
        if str(target_cfg.ai_role or "").strip() != "digital_member":
            raise HTTPException(status_code=400, detail="Target AI config must be digital_member")
        return target_cfg

    candidates = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.ai_role == "digital_member",
            AssistantAIConfig.enabled == True,
        ).order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
    ).all()
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No enabled digital_member available for task scheduling; provide target_ai_config_id or enable one",
        )
    manager = next(
        (cfg for cfg in candidates if str(cfg.digital_member_role or "").strip().lower() == "manager"),
        None,
    )
    return manager or candidates[0]
