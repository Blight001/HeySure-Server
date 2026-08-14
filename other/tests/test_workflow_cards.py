import pytest
from pathlib import Path

from api.services.workflows.compiler import WorkflowValidationError, compile_definition
from api.services.workflows.trace import definition_from_trace
from api.services.workflows.expression import (
    TemplateResolutionError,
    evaluate_expression,
    render_template,
)


def _definition():
    return {
        "schemaVersion": 1,
        "name": "read two files",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "startStepId": "read_first",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 5},
        "steps": {
            "read_first": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "name": "fs.read", "schemaDigest": "sha256:test"},
                "arguments": {"path": "${input.path}"},
                "saveAs": "first",
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {"content": "${steps.first.result.content}"},
    }


def test_compile_normalizes_and_hashes_valid_phase_one_definition():
    compiled = compile_definition(_definition())
    assert compiled["definition"]["limits"]["timeoutSeconds"] == 60
    assert compiled["digest"].startswith("sha256:")
    assert compiled["warnings"] == []


def test_compile_rejects_unsupported_steps():
    definition = _definition()
    definition["steps"]["finish"] = {
        "type": "script",
        "source": "return true",
    }
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("unsupported step type" in item for item in raised.value.errors)


def test_compile_rejects_unsafe_template_namespace():
    definition = _definition()
    definition["steps"]["read_first"]["arguments"]["path"] = "${environment.PATH}"
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("unknown namespace" in item for item in raised.value.errors)


def test_template_full_reference_preserves_type_and_interpolation_is_string():
    context = {
        "input": {"count": 3, "path": "a.txt"},
        "steps": {},
        "run": {},
        "device": {},
    }
    rendered = render_template(
        {"count": "${input.count}", "label": "file=${input.path}"},
        context,
    )
    assert rendered == {"count": 3, "label": "file=a.txt"}


def test_template_blocks_dunder_and_missing_values():
    context = {"input": {}, "steps": {}, "run": {}, "device": {}}
    with pytest.raises(TemplateResolutionError):
        render_template("${input.__class__}", context)
    with pytest.raises(TemplateResolutionError):
        render_template("${steps.not_finished.result}", context)


def test_template_supports_bounded_list_indexes():
    context = {
        "input": {},
        "steps": {"observe": {"result": {"items": [{"text": "alpha"}, {"text": "beta"}]}}},
        "run": {},
        "device": {},
    }
    assert render_template("${steps.observe.result.items[1].text}", context) == "beta"
    assert render_template("value=${steps.observe.result.items[0].text}", context) == "value=alpha"
    with pytest.raises(TemplateResolutionError, match="index 2 out of range"):
        render_template("${steps.observe.result.items[2].text}", context)


def test_compile_rejects_dynamic_or_negative_list_indexes():
    definition = _definition()
    definition["output"] = {"content": "${steps.first.result.items[-1].text}"}
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("invalid list index" in item for item in raised.value.errors)


def test_condition_expression_uses_safe_boolean_language():
    context = {
        "input": {"enabled": True, "name": "heysure-agent"},
        "steps": {}, "run": {}, "device": {},
    }
    assert evaluate_expression({
        "op": "and",
        "expressions": [
            {"op": "eq", "left": "${input.enabled}", "right": True},
            {"op": "startsWith", "left": "${input.name}", "right": "heysure"},
        ],
    }, context) is True
    with pytest.raises(TemplateResolutionError):
        evaluate_expression({"op": "eval", "value": "1+1"}, context)


def test_compile_accepts_condition_delay_ai_and_bounded_retry():
    definition = _definition()
    definition["steps"] = {
        "check": {
            "type": "condition",
            "expression": {"op": "exists", "value": "${input.path}"},
            "onTrue": "delay", "onFalse": "read",
        },
        "delay": {"type": "delay", "delaySeconds": 1, "next": "review"},
        "review": {
            "type": "ai", "prompt": "核对读取参数", "saveAs": "review_result",
            "next": "read",
        },
        "read": {
            "type": "mcp",
            "toolRef": {"namespace": "device", "name": "fs.read", "schemaDigest": "sha256:test"},
            "arguments": {"path": "${input.path}"},
            "saveAs": "read_result",
            "retryPolicy": {"maxAttempts": 3, "backoff": "exponential", "delaySeconds": 1},
            "next": "finish",
        },
        "finish": {"type": "end"},
    }
    definition["startStepId"] = "check"
    definition["output"] = {"content": "${steps.read_result.result.content}"}
    assert compile_definition(definition)["definition"]["steps"]["read"]["retryPolicy"]["maxAttempts"] == 3


def test_compile_rejects_removed_human_confirm_step():
    definition = _definition()
    definition["steps"]["read_first"]["next"] = "confirm"
    definition["steps"]["confirm"] = {"type": "confirm", "message": "continue?", "next": "finish"}
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("unsupported step type" in item for item in raised.value.errors)


def test_compile_rejects_literal_secret_in_arguments():
    definition = _definition()
    definition["steps"]["read_first"]["arguments"]["token"] = "hard-coded-secret"
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("literal sensitive value" in item for item in raised.value.errors)


def test_compile_rejects_branch_result_that_is_not_always_available():
    definition = _definition()
    definition["steps"] = {
        "choose": {
            "type": "condition",
            "expression": {"op": "eq", "left": "${input.path}", "right": "read"},
            "onTrue": "read", "onFalse": "finish",
        },
        "read": {
            "type": "mcp",
            "toolRef": {"namespace": "device", "name": "fs.read", "schemaDigest": "sha256:test"},
            "arguments": {"path": "${input.path}"},
            "saveAs": "content",
            "next": "finish",
        },
        "finish": {"type": "end"},
    }
    definition["startStepId"] = "choose"
    definition["output"] = {"content": "${steps.content.result.text}"}
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("unavailable on an end path" in item for item in raised.value.errors)


def test_compile_rejects_sensitive_result_projection():
    definition = _definition()
    definition["steps"]["read_first"]["resultProjection"] = ["headers.authorization"]
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("cannot persist sensitive field" in item for item in raised.value.errors)


def test_workflow_migration_places_claim_and_confirmation_columns_on_correct_tables():
    migration = (Path(__file__).parents[1] / "migrations" / "versions" / "c6d7e8f9a0b1_add_workflow_cards.py").read_text(
        encoding="utf-8"
    )
    card_block, step_block = migration.split('op.create_table(\n        "workflowsteprun"', 1)
    step_block, confirmation_block = step_block.split('op.create_table(\n        "workflowconfirmation"', 1)
    assert 'sa.Column("claim_owner"' not in card_block
    assert 'sa.Column("claim_owner"' in step_block
    assert 'sa.Column("claimed_at"' in step_block
    assert 'sa.Column("confirmation_type"' not in step_block
    assert 'sa.Column("confirmation_type"' in confirmation_block


def test_trace_to_draft_parameterizes_sensitive_values():
    definition = definition_from_trace(
        [{"tool": "browser.login", "arguments": {"account": "demo", "password": "plain-secret"}}],
        name="Login",
    )
    args = definition["steps"]["browser_login"]["arguments"]
    assert args["account"] == "demo"
    assert args["password"] == "${input.step_1_password}"
    assert "plain-secret" not in str(definition)
    assert definition["steps"]["browser_login"]["saveAs"] == "browser_login_result"
    assert definition["output"] == {"lastResult": "${steps.browser_login_result.result}"}
    assert compile_definition(definition)["definition"]["startStepId"] == "browser_login"


def test_browser_trace_declares_reload_initial_environment_contract():
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "arguments": {"ms": 500}},
        {"tool": "aifree.browser+observe", "arguments": {}},
    ], name="Stable browser flow")
    contract = definition["compatibility"]["initialEnvironment"]
    assert contract["resetStepId"] == "aifree_browser_tab_reload"
    assert contract["readyStepId"] == "aifree_browser_wait"
    assert definition["startStepId"] == "aifree_browser_control_acquire"
    assert definition["steps"]["aifree_browser_control_acquire"]["next"] == "aifree_browser_tab_reload"
    assert compile_definition(definition)["warnings"] == []


def test_browser_trace_stabilizes_unique_semantic_ref_from_fresh_observation():
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "device_id": "browser-a", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "device_id": "browser-a", "arguments": {"ms": 200}},
        {
            "tool": "aifree.browser+observe", "device_id": "browser-a", "arguments": {},
            "result": {"items": [
                {"id": "e14", "kind": "interactive", "tag": "button", "text": "暂存"},
                {"id": "e15", "kind": "interactive", "tag": "button", "text": "发布"},
            ]},
        },
        {
            "tool": "aifree.browser+action", "device_id": "browser-a",
            "arguments": {"action": "click", "ref": "e15"},
        },
    ], name="Stable recorded click")

    click = definition["steps"]["aifree_browser_action_click"]
    assert click["arguments"] == {"action": "click"}
    assert click["targetResolver"] == {
        "items": "${steps.aifree_browser_observe_result.result.items}",
        "text": "发布", "fields": ["text"], "exact": True,
        "kind": "interactive", "tag": "button",
    }
    assert not any(
        warning["code"] == "UNSTABLE_BROWSER_REF"
        for warning in definition.get("recordingWarnings", [])
    )
    assert compile_definition(definition)["warnings"] == []


@pytest.mark.parametrize(
    "recorded_result, expected_items_ref",
    [
        (
            {"result": {"items": [{"id": "e1", "kind": "interactive", "text": "提交"}]}},
            "${steps.aifree_browser_observe_result.result.result.items}",
        ),
        (
            {"result": {"result": {"items": [
                {"id": "e1", "kind": "interactive", "text": "提交"},
            ]}}},
            "${steps.aifree_browser_observe_result.result.result.result.items}",
        ),
    ],
)
def test_browser_trace_resolver_preserves_recorded_result_envelope_depth(
    recorded_result, expected_items_ref,
):
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "arguments": {"ms": 100}},
        {"tool": "aifree.browser+observe", "arguments": {}, "result": recorded_result},
        {"tool": "aifree.browser+action", "arguments": {"action": "click", "ref": "e1"}},
    ], name="Nested observation envelope")

    resolver = definition["steps"]["aifree_browser_action_click"]["targetResolver"]
    assert resolver["items"] == expected_items_ref
    assert compile_definition(definition)["warnings"] == []


def test_browser_trace_keeps_ambiguous_ref_and_marks_it_unstable():
    calls = [
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+observe", "arguments": {}, "result": {"items": [
            {"id": "e1", "kind": "interactive", "text": "发布"},
            {"id": "e2", "kind": "interactive", "text": "发布"},
        ]}},
        {"tool": "aifree.browser+action", "arguments": {"action": "click", "ref": "e2"}},
    ]

    definition = definition_from_trace(calls, name="Ambiguous recorded click")
    click = definition["steps"]["aifree_browser_action_click"]

    assert click["arguments"]["ref"] == "e2"
    assert "targetResolver" not in click
    assert {"code": "UNSTABLE_BROWSER_REF", "stepId": "aifree_browser_action_click"} in (
        definition["recordingWarnings"]
    )
    assert calls[-1]["arguments"]["ref"] == "e2"


def test_browser_trace_marks_explicitly_empty_observation():
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+observe", "arguments": {}, "result": {"items": []}},
    ], name="Empty observation")

    assert {"code": "EMPTY_BROWSER_OBSERVATION", "stepId": "aifree_browser_observe"} in (
        definition["recordingWarnings"]
    )
    assert compile_definition(definition)["warnings"] == []


def test_browser_trace_discards_redundant_empty_observation_without_changing_ready_step():
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "arguments": {"ms": 100}},
        {"tool": "aifree.browser+observe", "arguments": {}, "result": {"items": []}},
        {"tool": "aifree.browser+action", "arguments": {"action": "press_key", "key": "Escape"}},
    ], name="Drop redundant empty observation")

    assert "aifree_browser_observe" not in definition["steps"]
    assert definition["compatibility"]["initialEnvironment"]["readyStepId"] == "aifree_browser_wait"
    assert {"code": "DROPPED_EMPTY_BROWSER_OBSERVATION", "sourceSequence": 3} in (
        definition["recordingWarnings"]
    )
    assert compile_definition(definition)["warnings"] == []


def test_browser_trace_does_not_duplicate_existing_acquire_and_refreshes_after_release():
    definition = definition_from_trace([
        {"tool": "aifree.browser+control", "arguments": {"action": "acquire"}},
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "arguments": {"ms": 100}},
        {"tool": "aifree.browser+control", "arguments": {"action": "release"}},
        {"tool": "aifree.browser+action", "arguments": {"action": "press_key", "key": "Escape"}},
    ], name="Acquire lifecycle")

    acquire_steps = [
        step for step in definition["steps"].values()
        if step.get("toolRef", {}).get("name") == "aifree.browser+control"
        and step.get("arguments", {}).get("action") == "acquire"
    ]
    assert len(acquire_steps) == 2
    assert "aifree_browser_control_acquire_2" in definition["steps"]


def test_trace_semantic_ids_include_action_and_suffix_duplicates():
    definition = definition_from_trace([
        {"tool": "workspace.run+command", "arguments": {"action": "execute"}},
        {"tool": "workspace.run+command", "arguments": {"action": "execute"}},
        {"tool": "123.custom/tool", "arguments": {"action": "do thing"}},
    ], name="Semantic ids")

    assert list(definition["steps"]) == [
        "workspace_run_command_execute",
        "workspace_run_command_execute_2",
        "tool_123_custom_tool_do_thing",
        "finish",
    ]
    first = definition["steps"]["workspace_run_command_execute"]
    second = definition["steps"]["workspace_run_command_execute_2"]
    assert first["next"] == "workspace_run_command_execute_2"
    assert second["saveAs"] == "workspace_run_command_execute_2_result"
    assert definition["output"] == {"lastResult": "${steps.tool_123_custom_tool_do_thing_result.result}"}
    assert compile_definition(definition)["warnings"] == []


def test_trace_semantic_ids_remain_safe_when_names_are_long_or_reserved():
    long_tool = "9." + ("tool-name+" * 12)
    definition = definition_from_trace([
        {"tool": long_tool, "arguments": {"action": "execute"}},
        {"tool": long_tool, "arguments": {"action": "execute"}},
        {"tool": "finish", "arguments": {}},
    ], name="Safe semantic ids")

    step_ids = list(definition["steps"])
    assert step_ids[-2:] == ["finish_2", "finish"]
    assert step_ids[1].endswith("_2")
    assert all(len(step_id) <= 57 for step_id in step_ids[:-1])
    assert all(
        len(step["saveAs"]) <= 64
        for step in definition["steps"].values()
        if step["type"] == "mcp"
    )
    assert compile_definition(definition)["warnings"] == []


def test_browser_workflow_without_initial_environment_contract_is_rejected():
    definition = _definition()
    definition["steps"]["read_first"]["toolRef"]["name"] = "aifree.browser+observe"
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    message = " ".join(raised.value.errors)
    assert "initialEnvironment requires all three fields" in message
    assert "browser+tab reload/replace" in message
    assert "browser+wait/observe" in message


def test_browser_initial_environment_requires_reload_before_ready_step():
    definition = definition_from_trace([
        {"tool": "aifree.browser+tab", "arguments": {"action": "reload"}},
        {"tool": "aifree.browser+wait", "arguments": {"ms": 500}},
    ], name="Stable browser flow")
    definition["compatibility"]["initialEnvironment"]["resetStepId"] = "aifree_browser_wait"
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    message = " ".join(raised.value.errors)
    assert "initialEnvironment requires all three fields" in message
    assert "reset step must call browser+tab" in message


def test_ai_review_step_is_compiled_as_a_result_producer():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "review",
        "steps": {
            "review": {
                "type": "ai",
                "prompt": "核对并返回参数",
                "saveAs": "review_result", "timeoutSeconds": 60, "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 120, "maxTransitions": 5},
        "output": {"value": "${steps.review_result.result.value}"},
    }

    compiled = compile_definition(definition)

    assert compiled["definition"]["steps"]["review"]["type"] == "ai"


def test_workflow_card_router_does_not_expose_deprecation_action():
    from gateway.routers.workflow_cards import router

    assert all(not route.path.endswith("/deprecate") for route in router.routes)
