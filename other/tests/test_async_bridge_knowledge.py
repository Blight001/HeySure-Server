from api.runtime.async_bridge import run_async
from mcp_runtime.mcp.loader import reload_registry
from mcp_runtime.mcp.registry import registry


def test_run_async_knowledge_manage_list_thoughts():
    reload_registry()

    async def call():
        return await registry.call(
            "knowledge.manage",
            1,
            {"action": "list_thoughts", "params": {"limit": 1}},
            None,
        )

    for _ in range(3):
        payload = run_async(call())
        result = payload.get("result") if isinstance(payload, dict) else None
        assert isinstance(result, dict)
        assert "items" in result