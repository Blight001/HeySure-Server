"""Regression tests for the two bugs that made online device MCP tools vanish
from the prompt the model actually received (while the gateway preview still
showed them):

  Bug A — ``tools.engine.is_toolbox_gated_tool`` wrongly classified endpoint
          (device) / workshop tools as toolbox-gated, so an AI not bound to the
          toolbox had every online device tool stripped from its allow-list.

  Bug B — ``mcp_prompt_groups._agents_for_prompt_groups`` read the in-memory
          ``api.sio.agents`` socket registry (only populated in the gateway
          process) instead of the DB presence snapshot, so the ai-runtime worker
          (no sockets) rendered an empty "端侧设备 MCP" group.

Both are process-independence violations of the INVARIANT documented on
``chat_runtime_helpers.build_runtime_system_prompt_and_tools``. These tests lock
the fixes: device groups must be derived purely from DB-backed sources, with no
dependency on the in-memory socket registry.
"""

from mcp_runtime.mcp import registry
from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS
from tools.engine import TOOLBOX_GATE_EXEMPT, is_toolbox_gated_tool
from api.services.mcp import mcp_prompt_groups as g


def _registered_names():
    return {str(t.get("name") or "").strip() for t in registry.list_tools() if str(t.get("name") or "").strip()}


def test_is_toolbox_gated_only_matches_server_registry_tools():
    """Bug A: only server-registry tools may be toolbox-gated; endpoint/workshop
    tools (dynamic, not in the registry) and exempt introspection never are."""
    names = _registered_names()
    assert names, "MCP registry should be loaded in this process"

    # A real toolbox tool (registered, not library-bound, not exempt) stays gated.
    sample = next(
        n for n in sorted(names)
        if n not in TOOLBOX_GATE_EXEMPT and n not in LIBRARY_BOUND_TOOLS
    )
    assert is_toolbox_gated_tool(sample) is True

    # Endpoint / device tools are dynamic — never in the server registry — so they
    # must NOT be toolbox-gated regardless of namespace (incl. ones outside the
    # historical ENDPOINT_TOOL_PREFIXES list, e.g. speech.* / vision.* / remote_*).
    for endpoint_tool in [
        "browser_action", "screen.capture", "speech.speak",
        "vision.capture", "fs.read", "remote_control",
    ]:
        assert endpoint_tool not in names
        assert is_toolbox_gated_tool(endpoint_tool) is False, endpoint_tool

    # Introspection stays exempt so discovery works before any binding.
    assert is_toolbox_gated_tool("mcp.describe_tool") is False


def test_device_groups_are_built_from_db_presence(monkeypatch):
    """Bug B: with NO in-memory socket agents (the ai-runtime worker's reality),
    an online device recorded in DB presence must still produce a device group
    carrying its scoped tools — proving prompt assembly reads presence, not the
    socket registry."""
    device_id = "br-regression-1"
    browser_caps = {"browser_action", "browser_tab", "browser_observe"}

    # DB-backed device source (what _agents_for_prompt_groups must consult).
    monkeypatch.setattr(
        "api.devices.presence.online_devices_for_config",
        lambda user_id, ai_config_id: [(device_id, "browser", set(browser_caps))],
    )
    # Per-agent scope opens exactly these tools.
    monkeypatch.setattr(g, "get_scope", lambda user_id, did: set(browser_caps) if did == device_id else None)
    monkeypatch.setattr(g, "_config_selected_tool_names", lambda ai_config_id, user_id: set())
    monkeypatch.setattr(g, "is_endpoint_agent_tool", lambda name: name.startswith("browser_"))
    # Keep the library-binding probe off the DB.
    monkeypatch.setattr("api.devices.workshop_bindings.config_bound_to_library", lambda u, c: False)

    groups = g.build_prompt_tool_groups(
        user_id=1,
        ai_config_id=99,
        prompt_tools=[{"name": n, "mcpSource": "browser", "description": n} for n in browser_caps],
        allowed_tools=set(browser_caps),
    )

    device_groups = [grp for grp in groups if grp.get("groupKind") == "device"]
    assert len(device_groups) == 1, groups
    grp = device_groups[0]
    assert grp.get("deviceId") == device_id
    assert {t["name"] for t in grp["tools"]} == browser_caps
    # The empty fallback group must NOT appear when a real device is present.
    assert not any(x.get("groupKey") == "device:none" for x in groups)


def test_empty_device_fallback_when_no_presence(monkeypatch):
    """No online device in presence → a single empty '端侧设备 MCP' group, and
    crucially no crash / no dependency on the socket registry."""
    monkeypatch.setattr(
        "api.devices.presence.online_devices_for_config",
        lambda user_id, ai_config_id: [],
    )
    monkeypatch.setattr(g, "_config_selected_tool_names", lambda ai_config_id, user_id: set())
    monkeypatch.setattr("api.devices.workshop_bindings.config_bound_to_library", lambda u, c: False)

    groups = g.build_prompt_tool_groups(
        user_id=1, ai_config_id=99, prompt_tools=[], allowed_tools=set(),
    )
    fallback = [x for x in groups if x.get("groupKey") == "device:none"]
    assert len(fallback) == 1
    assert fallback[0]["tools"] == []
