"""Shared AI-to-AI management authority rules (Phase 2 governance tree).

A manager AI may only act on (schedule tasks for / edit prompts of) AIs it is
authorized to manage. Authority derives from explicit governance links:

    parent_ai_config_id        direct superior
    management_scope           self / children / project / global

To stay backward compatible with deployments that have not configured any
governance links yet, authority falls back to permissive (legacy) behaviour
when no AI under the user has a parent_ai_config_id set.
"""

from typing import Optional

from sqlmodel import Session, func, select

from ..models import AssistantAIConfig


def user_has_governance_tree(session: Session, user_id: int) -> bool:
    count = session.exec(
        select(func.count())
        .select_from(AssistantAIConfig)
        .where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.parent_ai_config_id.is_not(None),
        )
    ).one()
    try:
        return int(count) > 0
    except Exception:
        return bool(count)


def can_manage(
    session: Session,
    manager: AssistantAIConfig,
    target: AssistantAIConfig,
) -> bool:
    if manager.id == target.id:
        return True

    scope = str(getattr(manager, "management_scope", "") or "self").strip().lower()
    is_manager_role = str(manager.digital_member_role or "").strip().lower() == "manager"
    is_admin = str(manager.ai_role or "").strip() == "assistant_admin"

    if not is_manager_role and not is_admin:
        return False

    if scope == "global" or is_admin:
        return True

    # Direct child link always grants authority.
    if target.parent_ai_config_id is not None and int(target.parent_ai_config_id) == int(manager.id):
        return True

    if scope == "project":
        manager_project = str(manager.project_id or "").strip()
        target_project = str(target.project_id or "").strip()
        if manager_project and manager_project == target_project:
            return True

    return False


def assert_can_manage_or_legacy(
    session: Session,
    user_id: int,
    manager: AssistantAIConfig,
    target: AssistantAIConfig,
) -> Optional[str]:
    """Return None when allowed, or a denial reason string when blocked.

    Permissive when the user has not configured any governance tree yet.
    """
    if manager.id == target.id:
        return None
    if can_manage(session, manager, target):
        return None
    if not user_has_governance_tree(session, user_id):
        return None  # legacy: no tree configured, keep prior open behaviour
    return (
        f"AI '{manager.name}' (#{manager.id}) is not authorized to manage "
        f"AI '{target.name}' (#{target.id}). Configure parent_ai_config_id "
        f"or management_scope to grant authority."
    )
