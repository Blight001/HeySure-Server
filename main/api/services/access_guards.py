"""Ownership / existence guards shared by routers across the server processes.

These collapse the ``session.get(...) → 404 if missing/not-owned`` boilerplate
that was copy-pasted across the AI-config, task and bot routers into one place,
so the lookup + authorization rule lives in a single spot (DRY / 单一职责).
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session

from api.models import AssistantAIConfig


def get_ai_config_or_404(
    session: Session,
    config_id: object,
    user_id: Optional[int] = None,
    *,
    detail: str = "AI config not found",
) -> AssistantAIConfig:
    """Load an :class:`AssistantAIConfig` or raise 404.

    When ``user_id`` is given the row must also belong to that user (the common
    ownership guard); pass ``None`` to only assert existence.
    """
    cfg = session.get(AssistantAIConfig, config_id)
    if not cfg or (user_id is not None and cfg.user_id != user_id):
        raise HTTPException(status_code=404, detail=detail)
    return cfg
