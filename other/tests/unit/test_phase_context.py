from types import SimpleNamespace

from ai_runtime.inference import phase_context


def test_phase_compaction_contains_only_phase_status_and_summary():
    text = phase_context.build_phase_compaction_text(
        {"seq": 1, "title": "验证部署", "status": "completed", "summary": "健康检查通过，未发现遗留问题。"},
        [("shell.exec", True), ("workspace.run+command", False)],
    )
    assert text == "[系统提示 · 阶段2 已完成] 验证部署\n阶段小结：健康检查通过，未发现遗留问题。"
    assert "MCP 调用状态" not in text
    assert "深度思考" not in text
    assert "详细结果" not in text


def test_phase_compaction_keeps_legacy_optional_status_argument():
    text = phase_context.build_phase_compaction_text({"seq": 0, "title": "失败阶段", "status": "failed"}, None)
    assert text == "[系统提示 · 阶段1 未达成] 失败阶段"


class _Rows:
    def __init__(self, rows): self.rows = rows
    def all(self): return self.rows


class _Session:
    def __init__(self, rows): self.rows, self.added = rows, []
    def exec(self, _statement): return _Rows(self.rows)
    def add(self, row): self.added.append(row)


def test_mark_phase_messages_compressed_tags_without_deleting_or_overwriting():
    assistant = SimpleNamespace(role="assistant", tags="mcp_assistant_call", content="原始回答", total_tokens=42)
    tool = SimpleNamespace(role="system", tags="mcp_tool_call", content="原始工具结果", total_tokens=18)
    user = SimpleNamespace(role="user", tags="", content="用户原话", total_tokens=7)
    session = _Session([assistant, tool, user])
    marked = phase_context.mark_phase_messages_compressed(
        session, user_id=1, ai_config_id=2, ai_kind="assistant", session_id="session-1",
        since_ts=0, until_ts=9999999999,
    )
    assert marked == 2
    assert assistant.content == "原始回答"
    assert tool.content == "原始工具结果"
    assert user.content == "用户原话"
    assert "compressed_away" in assistant.tags
    assert "compressed_away" in tool.tags
    assert user.tags == ""
    assert session.added == [assistant, tool]
