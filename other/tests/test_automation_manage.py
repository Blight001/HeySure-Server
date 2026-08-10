from types import SimpleNamespace

from mcp_runtime.mcp.builtin_catalog import BUILTIN_TOOLS
from mcp_runtime.mcp.permissions import requires_library_binding
from tools.automation import (
    AUTOMATION_MANAGE_SCHEMA,
    _card_visible,
    _creation_tags,
    _is_admin_role,
    _pending_confirmation_guidance,
    _updated_tags,
)


def _card(tags):
    import json
    return SimpleNamespace(tags_json=json.dumps(tags))


def test_only_admin_and_assistant_admin_roles_have_global_card_access():
    assert _is_admin_role("admin")
    assert _is_admin_role("assistant_admin")
    assert not _is_admin_role("digital_member")
    assert not _is_admin_role("manager")


def test_ai_created_card_gets_stable_owner_tag():
    assert _creation_tags(["report"], 12) == ["report", "ai_owner:12"]
    assert _creation_tags(["ai_owner:99", "report"], 12) == ["report", "ai_owner:12"]


def test_ai_can_only_see_own_or_public_card():
    assert _card_visible(_card([]), 12)
    assert _card_visible(_card(["report"]), 12)
    assert _card_visible(_card(["ai_owner:12"]), 12)
    assert not _card_visible(_card(["ai_owner:99"]), 12)
    assert _card_visible(_card(["ai_owner:99"]), None)


def test_edit_preserves_owner_and_cannot_relabel_card():
    card = _card(["report", "ai_owner:12"])
    assert _updated_tags(card, ["edited", "ai_owner:99"]) == ["edited", "ai_owner:12"]


def test_registry_exposes_one_aggregated_automation_toolbox_mcp():
    names = [tool.name for tool in BUILTIN_TOOLS if tool.name.startswith("automation.")]
    assert names == ["automation.manage"]
    assert not requires_library_binding("automation.manage")
    actions = set(AUTOMATION_MANAGE_SCHEMA["properties"]["action"]["enum"])
    assert {"create", "edit", "start", "pause", "resume", "cancel", "delete"} <= actions


def test_pending_confirmation_guidance_distinguishes_user_and_ai_actions():
    explicit = SimpleNamespace(
        id="confirm-user",
        step_id="dangerous",
        confirmation_type="explicit",
        risk_summary="需要真人确认",
        expires_at=123.0,
        ai_config_id=None,
    )
    user_guidance = _pending_confirmation_guidance(explicit, 19)
    assert user_guidance["required_action"] == "user_confirmation"
    assert user_guidance["can_respond"] is False

    ai_review = SimpleNamespace(
        id="confirm-ai",
        step_id="review",
        confirmation_type="ai_review",
        risk_summary="请 AI 核对",
        expires_at=456.0,
        ai_config_id=19,
    )
    ai_guidance = _pending_confirmation_guidance(ai_review, 19)
    assert ai_guidance["required_action"] == "automation.manage:respond"
    assert ai_guidance["can_respond"] is True
