from connector_runtime.dispatch.desktop_device_tools import agent_endpoint_tools
from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS


def test_builtin_library_keeps_governance_tools_in_device_scope():
    agent = {
        "id": "workshop_builtin_7",
        "source": "builtin",
        "deviceType": "workshop",
        "isWorkshop": True,
        "capabilities": sorted(LIBRARY_BOUND_TOOLS),
    }

    assert agent_endpoint_tools(agent) == set(LIBRARY_BOUND_TOOLS)


def test_external_workshop_cannot_report_library_governance_tools():
    agent = {
        "id": "external_workshop_7",
        "source": "socket",
        "deviceType": "workshop",
        "isWorkshop": True,
        "capabilities": [*sorted(LIBRARY_BOUND_TOOLS), "evolution.review"],
    }

    assert agent_endpoint_tools(agent) == {"evolution.review"}
