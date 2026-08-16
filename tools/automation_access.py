"""Role and ownership policy for automation cards."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import Session, select

from api.models import AssistantAIConfig, WorkflowCard


OWNER_TAG_PREFIX = WorkflowCard.AI_OWNER_TAG_PREFIX


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _is_admin_role(role: Any) -> bool:
    return str(role or "").strip() in {"admin", "assistant_admin"}


def _admin_actor(session: Session, user_id: int, ai_config_id: Optional[int]) -> bool:
    if not ai_config_id:
        return True
    config = session.exec(select(AssistantAIConfig).where(
        AssistantAIConfig.id == int(ai_config_id),
        AssistantAIConfig.user_id == user_id,
    )).first()
    return bool(config and _is_admin_role(config.ai_role))


def _public_card_creator(session: Session, user_id: int, ai_config_id: Optional[int]) -> bool:
    if not ai_config_id:
        return True
    config = session.exec(select(AssistantAIConfig).where(
        AssistantAIConfig.id == int(ai_config_id),
        AssistantAIConfig.user_id == user_id,
    )).first()
    return bool(config and (
        _is_admin_role(config.ai_role)
        or str(config.digital_member_role or "").strip().lower() == "manager"
    ))


def _card_visible(card: WorkflowCard, ai_config_id: Optional[int]) -> bool:
    tags = _load(card.tags_json, [])
    scope = getattr(card, "access_scope", None)
    if not scope:
        scope = "owner" if WorkflowCard.ai_owner_ids(tags) else "all"
    return WorkflowCard.accessible_to_ai(
        access_scope=scope,
        allowed_ai_config_ids=_load(getattr(card, "allowed_ai_config_ids_json", "[]"), []),
        tags=tags,
        ai_config_id=ai_config_id,
    )


def _creation_tags(tags: Any, ai_config_id: Optional[int]) -> list[str]:
    cleaned = [
        str(item).strip()
        for item in (tags if isinstance(tags, list) else [])
        if str(item).strip() and not str(item).strip().lower().startswith(OWNER_TAG_PREFIX)
    ]
    if ai_config_id:
        cleaned.append(f"{OWNER_TAG_PREFIX}{int(ai_config_id)}")
    return list(dict.fromkeys(cleaned))


def _updated_tags(card: WorkflowCard, tags: Any) -> list[str]:
    owners = [
        str(item).strip()
        for item in _load(card.tags_json, [])
        if str(item).strip().lower().startswith(OWNER_TAG_PREFIX)
    ]
    return list(dict.fromkeys(_creation_tags(tags, None) + owners))


def _pending_ai_review_guidance(row: Any, ai_config_id: Optional[int]) -> dict[str, Any]:
    confirmation_type = str(getattr(row, "confirmation_type", "explicit") or "explicit")
    assigned_ai_id = getattr(row, "ai_config_id", None)
    can_respond = bool(
        ai_config_id
        and assigned_ai_id == int(ai_config_id)
        and confirmation_type == "ai_review"
    )
    import time
    expires_at = getattr(row, "expires_at", None)
    expires_in = max(0, int(float(expires_at) - time.time())) if expires_at else None
    return {
        "id": str(getattr(row, "id", "") or ""),
        "step_id": str(getattr(row, "step_id", "") or ""),
        "type": confirmation_type,
        "risk_summary": str(getattr(row, "risk_summary", "") or ""),
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
        "recommended_response_deadline": expires_at,
        "assigned_ai_config_id": assigned_ai_id,
        "can_respond": can_respond,
        "required_action": "automation.manage:respond" if can_respond else "unavailable",
        "guidance": (
            "调用 automation.manage action=respond，并提供 approved。"
            if can_respond
            else "该 AI 审核交互不属于当前 AI，不能响应。"
        ),
    }
