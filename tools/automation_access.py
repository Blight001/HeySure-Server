"""Role and ownership policy for automation cards."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import Session, select

from api.models import AssistantAIConfig, WorkflowCard


OWNER_TAG_PREFIX = "ai_owner:"


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


def _owner_ids(tags: Any) -> set[str]:
    return {
        str(item).strip()[len(OWNER_TAG_PREFIX):]
        for item in (tags if isinstance(tags, list) else [])
        if str(item).strip().lower().startswith(OWNER_TAG_PREFIX)
    }


def _card_visible(card: WorkflowCard, ai_config_id: Optional[int]) -> bool:
    if not ai_config_id:
        return True
    owners = _owner_ids(_load(card.tags_json, []))
    return not owners or str(ai_config_id) in owners


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
