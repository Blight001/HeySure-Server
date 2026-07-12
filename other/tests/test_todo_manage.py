from tools import task_plan as todo_tools


def test_todo_manage_routes_all_operations_through_one_handler(monkeypatch):
    monkeypatch.setattr(todo_tools, "_plan_create", lambda *args: {"action": "create"})
    monkeypatch.setattr(todo_tools, "_plan_get", lambda *args: {"action": "get"})
    monkeypatch.setattr(todo_tools, "_phase_complete", lambda *args: {"action": "edit"})
    monkeypatch.setattr(todo_tools, "_plan_delete", lambda *args: {"action": "delete"})

    assert todo_tools._todo_manage(1, {"action": "create"}, 2) == {"action": "create"}
    assert todo_tools._todo_manage(1, {"action": "get"}, 2) == {"action": "get"}
    assert todo_tools._todo_manage(1, {"action": "edit"}, 2) == {"action": "edit"}
    assert todo_tools._todo_manage(1, {"action": "delete"}, 2) == {"action": "delete"}


def test_todo_manage_infers_legacy_plan_argument_shapes(monkeypatch):
    monkeypatch.setattr(todo_tools, "_plan_create", lambda *args: {"action": "create"})
    monkeypatch.setattr(todo_tools, "_phase_complete", lambda *args: {"action": "edit"})

    assert todo_tools._todo_manage(1, {"goal": "g", "phases": []}, 2) == {"action": "create"}
    assert todo_tools._todo_manage(1, {"status": "completed"}, 2) == {"action": "edit"}


def test_todo_manage_is_the_only_registered_plan_mcp():
    from mcp_runtime.mcp import registry

    names = {str(tool["name"]) for tool in registry.list_tools()}
    assert "todo.manage" in names
    assert "plan.create" not in names
    assert "plan.phase+complete" not in names
    assert "plan.finish" not in names


def test_legacy_plan_names_map_to_todo_manage():
    from api.services.mcp.mcp_tool_aliases import normalize_legacy_tool_name, resolve_tool_name

    assert normalize_legacy_tool_name("plan.create") == "todo.manage"
    assert normalize_legacy_tool_name("plan.phase+complete") == "todo.manage"
    assert normalize_legacy_tool_name("plan.phase_complete") == "todo.manage"
    assert normalize_legacy_tool_name("plan.finish") == "todo.manage"
    assert resolve_tool_name("plan_create", {"todo.manage"}) == "todo.manage"
    assert resolve_tool_name("plan_phase_complete", {"todo.manage"}) == "todo.manage"


def test_stored_task_prompts_are_upgraded_to_todo_manage():
    from api.services.knowledge.kb_store import normalize_todo_mcp_prompt

    upgraded = normalize_todo_mcp_prompt(
        "plan.create then plan.phase_complete then plan.finish"
    )
    assert upgraded == (
        "todo.manage(action=create) then todo.manage(action=edit) "
        "then todo.manage(action=edit)"
    )
