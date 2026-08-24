from connector_runtime.dispatch.automation_card_compat import (
    local_aifree_tool_name,
    normalize_automation_card_arguments,
)


def test_local_aifree_tool_name_accepts_endpoint_and_codex_aliases():
    assert local_aifree_tool_name("aifree.browser+action") == "browser_action"
    assert local_aifree_tool_name("aifree_browser-file") == "browser_file"
    assert (
        local_aifree_tool_name("mcp__heysure_member__aifree_browser_tab")
        == "browser_tab"
    )
    assert local_aifree_tool_name("workspace.run+command") == "workspace.run+command"


def test_card_write_normalizes_inner_mcp_steps_without_mutating_input():
    arguments = {
        "action": "write",
        "cardData": {
            "name": "publish",
            "steps": [
                {
                    "type": "mcp",
                    "tool": "mcp__heysure_member__aifree_browser_tab",
                    "arguments": {"action": "navigate"},
                },
                {"type": "delay", "ms": 100},
                {"type": "mcp", "tool": "aifree.browser+action"},
            ],
        },
    }

    normalized = normalize_automation_card_arguments("aifree.manage+card", arguments)

    assert [step.get("tool") for step in normalized["cardData"]["steps"]] == [
        "browser_tab",
        None,
        "browser_action",
    ]
    assert (
        arguments["cardData"]["steps"][0]["tool"]
        == "mcp__heysure_member__aifree_browser_tab"
    )


def test_card_step_edits_are_normalized_for_old_clients():
    patched = normalize_automation_card_arguments(
        "mcp__heysure_member__aifree_manage_card",
        {
            "action": "patch_step",
            "stepPatch": {"type": "mcp", "tool": "aifree.browser+observe"},
        },
    )
    inserted = normalize_automation_card_arguments(
        "manage_card",
        {
            "action": "insert_step",
            "stepData": {"type": "mcp", "tool": "aifree_browser-wait"},
        },
    )

    assert patched["stepPatch"]["tool"] == "browser_observe"
    assert inserted["stepData"]["tool"] == "browser_wait"


def test_read_and_unrelated_endpoint_calls_are_untouched():
    read_args = {"action": "get", "id": "card-1"}
    other_args = {
        "action": "write",
        "cardData": {"steps": [{"type": "mcp", "tool": "aifree.browser+tab"}]},
    }

    assert normalize_automation_card_arguments("aifree.manage+card", read_args) is read_args
    assert normalize_automation_card_arguments("aifree.browser+action", other_args) is other_args
