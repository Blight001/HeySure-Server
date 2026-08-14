"""Resolve the filesystem workspace owned by one AI member."""

import os
from typing import Optional

from sqlmodel import Session, select

from api.core.config import ai_workspace_dirname, user_workspace_dir
from api.database import engine
from api.models import AssistantAIConfig


def member_workspace_dir(
    user_id: int,
    ai_config_id: Optional[int],
    *,
    create: bool = False,
) -> str:
    """Return the existing workspace scope used by MCP and persisted files.

    Every configured AI is isolated in its own named subdirectory. Role fields
    are labels and never widen filesystem access.
    """

    user_root = os.path.abspath(user_workspace_dir(int(user_id)))
    root = user_root
    if ai_config_id:
        with Session(engine) as session:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == int(user_id),
                    AssistantAIConfig.id == int(ai_config_id),
                )
            ).first()
        if cfg is None:
            root = os.path.join(user_root, f"{int(ai_config_id)}-ai")
        else:
            root = os.path.join(
                user_root,
                ai_workspace_dirname(cfg.id, cfg.name, cfg.ai_role),
            )
    root = os.path.abspath(root)
    if create:
        os.makedirs(root, exist_ok=True)
    return root
