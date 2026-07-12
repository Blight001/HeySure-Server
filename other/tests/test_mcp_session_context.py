import json

from api.services.chat.mcp_session_context import (
    compact_mcp_history_messages,
    parse_mcp_tool_bubble,
)


def _bubble(result: str) -> str:
    return (
        "[MCP工具]\n"
        "工具: workspace.search\n"
        "状态: 成功\n\n"
        "[参数]\n"
        '{"query":"保留完整参数","limit":5}\n\n'
        "[结果]\n"
        f"{result}"
    )


def test_parse_mcp_tool_bubble_preserves_arguments():
    parsed = parse_mcp_tool_bubble(_bubble("ok"))
    assert parsed is not None
    assert parsed["tool"] == "workspace.search"
    assert parsed["arguments"] == {"query": "保留完整参数", "limit": 5}
    assert parsed["result"] == "ok"


def test_compact_history_keeps_native_call_and_limits_only_result():
    messages = compact_mcp_history_messages(42, _bubble("x" * 150), 100)
    assert [item["role"] for item in messages] == ["assistant", "tool"]
    call = messages[0]["tool_calls"][0]
    assert call["id"] == "history_mcp_42"
    assert call["function"]["name"] == "workspace__search"
    assert json.loads(call["function"]["arguments"]) == {
        "query": "保留完整参数",
        "limit": 5,
    }
    assert messages[1]["tool_call_id"] == call["id"]
    assert messages[1]["content"].startswith("x" * 100)
    assert "历史返回已缩减" in messages[1]["content"]


def test_compact_history_does_not_mark_short_result_as_truncated():
    messages = compact_mcp_history_messages(1, _bubble("short"), 100)
    assert messages[1]["content"] == "short"


def test_disabled_compaction_keeps_full_result():
    full = "x" * 150
    messages = compact_mcp_history_messages(1, _bubble(full), 0)
    assert messages[1]["content"] == full
