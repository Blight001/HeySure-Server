from types import SimpleNamespace

from api.chat_runtime.chat_runtime_helpers import _digital_society_roster_text
from api.services.mcp.mcp_tool_aliases import resolve_tool_name
from mcp_runtime.mcp import registry
from tools import communication


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec(self, _statement):
        return _Rows(self._rows)


def _agent(agent_id, name, *, lifecycle_status="working", role="digital_member"):
    return SimpleNamespace(
        id=agent_id,
        user_id=1,
        name=name,
        lifecycle_status=lifecycle_status,
        ai_role=role,
        digital_member_role="member",
        is_librarian=False,
    )


def test_roster_exposes_peer_ids_but_skips_dead_agents():
    text = _digital_society_roster_text(
        _Session([_agent(1, "自己"), _agent(2, "同伴"), _agent(3, "已离开", lifecycle_status="dead")]),
        user_id=1,
        self_ai_config_id=1,
    )

    assert "你的 ai_config_id 是 1（自己）" in text
    assert "ID 2：同伴" in text
    assert "已离开" not in text


def test_send_to_ai_can_resolve_peer_by_name(monkeypatch):
    rows = [_agent(1, "自己"), _agent(2, "同伴")]
    monkeypatch.setattr(communication, "Session", lambda _engine: _Session(rows))

    assert communication._resolve_target_ai_id_by_name(1, "同伴") == 2


def test_mcp_native_name_variants_resolve_to_the_same_registered_tool():
    candidates = {"mcp.describe+tool", "workspace.run+command", "message.send+to"}

    assert resolve_tool_name("mcp_describe-tool", candidates) == "mcp.describe+tool"
    assert resolve_tool_name("mcp_describe_tool", candidates) == "mcp.describe+tool"
    assert resolve_tool_name("mcp.describe_tool", candidates) == "mcp.describe+tool"
    assert resolve_tool_name("workspace_run-command", candidates) == "workspace.run+command"
    # 合并前的两个旧工具名（含底线旧写法）都应归一到统一的 message.send+to。
    assert resolve_tool_name("message.send+to+user", candidates) == "message.send+to"
    assert resolve_tool_name("message.send+to+ai", candidates) == "message.send+to"
    assert resolve_tool_name("message.send_to_user", candidates) == "message.send+to"
    assert resolve_tool_name("message_send-to", candidates) == "message.send+to"


def test_registered_mcp_names_do_not_contain_internal_underscores():
    names = {str(tool["name"]) for tool in registry.list_tools()}

    assert names
    assert all("_" not in name for name in names)
