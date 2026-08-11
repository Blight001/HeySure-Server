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


def _card_visible(card: WorkflowCard, ai_config_id: Optional[int]) -> bool:
    return WorkflowCard.tags_visible_to_ai(_load(card.tags_json, []), ai_config_id)


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


def _pending_confirmation_guidance(row: Any, ai_config_id: Optional[int]) -> dict[str, Any]:
    interaction_types = {"ai_review", "user_via_ai", "user_via_ai_dispatch"}
    confirmation_type = str(getattr(row, "confirmation_type", "explicit") or "explicit")
    assigned_ai_id = getattr(row, "ai_config_id", None)
    can_respond = bool(
        ai_config_id
        and assigned_ai_id == int(ai_config_id)
        and confirmation_type in interaction_types
    )
    return {
        "id": str(getattr(row, "id", "") or ""),
        "step_id": str(getattr(row, "step_id", "") or ""),
        "type": confirmation_type,
        "risk_summary": str(getattr(row, "risk_summary", "") or ""),
        "expires_at": getattr(row, "expires_at", None),
        "assigned_ai_config_id": assigned_ai_id,
        "can_respond": can_respond,
        "required_action": "automation.manage:respond" if can_respond else "user_confirmation",
        "guidance": (
            "调用 automation.manage action=respond，并提供 approved。"
            if can_respond
            else "此门禁必须由真人用户确认；AI 不应反复调用 respond。"
        ),
    }
