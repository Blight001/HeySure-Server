"""Shared base for the ``/api/ai`` router family: defines the ``APIRouter`` and
shared helpers (prompt section stripping, default ``system_auto_control`` blobs,
role normalization, task-owner resolution) used by the ai_* route modules."""

IS_ROUTER_ENTRY = False

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from sqlmodel import Session

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
    _ = value
    return "digital_member"

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
    _ = (session, user_id, payload_body)
    if str(caller_cfg.ai_role or "").strip() != "digital_member":
        raise HTTPException(status_code=400, detail="Only digital_member supports task scheduler")
    return caller_cfg
