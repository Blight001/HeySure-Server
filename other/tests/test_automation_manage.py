from types import SimpleNamespace

from mcp_runtime.mcp.builtin_catalog import BUILTIN_TOOLS
from mcp_runtime.mcp.permissions import requires_library_binding
from tools import automation
from tools.automation import (
    AUTOMATION_MANAGE_SCHEMA,
    _card_visible,
    _creation_tags,
    _is_admin_role,
    _pending_ai_review_guidance,
    _updated_tags,
    _edit_card,
    _created_card_reference,
)
import pytest
from fastapi import HTTPException


def _card(tags):
    import json
    return SimpleNamespace(tags_json=json.dumps(tags))


def _scoped_card(scope, allowed_ids, tags=None):
    import json
    return SimpleNamespace(
        tags_json=json.dumps(tags or []),
        access_scope=scope,
        allowed_ai_config_ids_json=json.dumps(allowed_ids),
    )


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


def test_explicit_card_scope_can_allow_all_owner_or_selected_members():
    assert _card_visible(_scoped_card("all", []), 12)
    assert _card_visible(_scoped_card("owner", [], ["ai_owner:12"]), 12)
    assert not _card_visible(_scoped_card("owner", [], ["ai_owner:12"]), 13)
    assert _card_visible(_scoped_card("selected", [12, 14]), 14)
    assert not _card_visible(_scoped_card("selected", [12, 14]), 13)


def test_edit_preserves_owner_and_cannot_relabel_card():
    card = _card(["report", "ai_owner:12"])
    assert _updated_tags(card, ["edited", "ai_owner:99"]) == ["edited", "ai_owner:12"]


def test_registry_exposes_one_aggregated_automation_toolbox_mcp():
    names = [tool.name for tool in BUILTIN_TOOLS if tool.name.startswith("automation.")]
    assert names == ["automation.manage"]
    assert not requires_library_binding("automation.manage")
    actions = set(AUTOMATION_MANAGE_SCHEMA["properties"]["action"]["enum"])
    assert {"create", "edit", "start", "pause", "resume", "cancel", "delete"} <= actions
    assert {"import", "clone", "retry", "export"}.isdisjoint(actions)


@pytest.mark.parametrize("action", ["import", "clone", "retry", "export"])
def test_removed_automation_actions_are_rejected(monkeypatch, action):
    monkeypatch.setattr(automation, "_require_enabled", lambda **kwargs: None)
    with pytest.raises(HTTPException, match="unsupported automation.manage action"):
        automation._automation_manage(1, {"action": action}, 2)


def test_automation_schema_prefers_recording_and_scopes_manual_edits_to_patches():
    schema_description = AUTOMATION_MANAGE_SCHEMA["description"]
    action_description = AUTOMATION_MANAGE_SCHEMA["properties"]["action"]["description"]
    patch_description = AUTOMATION_MANAGE_SCHEMA["properties"]["operations"]["description"]
    definition_description = AUTOMATION_MANAGE_SCHEMA["properties"]["definition"]["description"]
    assert "record_start" in schema_description and "record_stop(create_card=true)" in schema_description
    assert "不要默认使用 create/from_trace" in action_description
    assert "小细节" in patch_description
    assert "不要凭空手写复杂流程" in definition_description
    assert "replace_definition" in AUTOMATION_MANAGE_SCHEMA["properties"]["action"]["enum"]
    assert "结构性重构" in AUTOMATION_MANAGE_SCHEMA["properties"]["action"]["description"]
    assert "元数据仍使用 edit" in definition_description
    assert "不创建版本" in AUTOMATION_MANAGE_SCHEMA["properties"]["dry_run"]["description"]
    assert AUTOMATION_MANAGE_SCHEMA["properties"]["compact_recording"]["default"] is True
    path_description = AUTOMATION_MANAGE_SCHEMA["properties"]["operations"]["items"]["properties"]["path"]["description"]
    assert "不要带 /definition 前缀" in path_description
    assert "/inputSchema/properties/prompt" in path_description


def test_record_stop_created_card_reference_is_immediately_actionable():
    card = SimpleNamespace(id="wcard-1", latest_version_id="version-2")
    version = SimpleNamespace(definition_digest="sha256:definition")

    assert _created_card_reference(card, version) == {
        "card_id": "wcard-1",
        "version_id": "version-2",
        "definition_digest": "sha256:definition",
    }


def test_automation_description_teaches_ai_all_supported_node_shapes():
    tool = next(item for item in BUILTIN_TOOLS if item.name == "automation.manage")
    definition_description = AUTOMATION_MANAGE_SCHEMA["properties"]["definition"]["description"]
    for description in (tool.description, definition_description):
        assert "mcp" in description
        assert "condition" in description
        assert "delay" in description
        assert "type:'ai'" in description
        assert "end" in description
        assert "完整步骤轨迹" in description
        assert "action=respond" in description
        assert "__workflow.ai_intervention" not in description
        assert "${steps.<saveAs>.result.<字段>}" in description


def test_removed_human_confirmation_is_not_actionable_by_ai():
    explicit = SimpleNamespace(
        id="confirm-user",
        step_id="dangerous",
        confirmation_type="explicit",
        risk_summary="需要真人确认",
        expires_at=123.0,
        ai_config_id=None,
    )
    user_guidance = _pending_ai_review_guidance(explicit, 19)
    assert user_guidance["required_action"] == "unavailable"
    assert user_guidance["can_respond"] is False

    ai_review = SimpleNamespace(
        id="confirm-ai",
        step_id="review",
        confirmation_type="ai_review",
        risk_summary="请 AI 核对",
        expires_at=456.0,
        ai_config_id=19,
    )
    ai_guidance = _pending_ai_review_guidance(ai_review, 19)
    assert ai_guidance["required_action"] == "automation.manage:respond"
    assert ai_guidance["can_respond"] is True


def test_ai_edit_cannot_replace_an_entire_existing_definition():
    with pytest.raises(HTTPException, match="FULL_DEFINITION_REPLACE_DISABLED"):
        _edit_card(None, _card([]), {"definition": {"steps": {}}}, 1)
