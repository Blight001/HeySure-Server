import os
import tempfile

from api.services.knowledge import kb_store
from mcp_runtime.mcp.permissions import ROLE_MEMBER, tool_min_role
from mcp_runtime.mcp.registry import registry


def test_knowledge_search_registered_as_read_only_tool():
    tool = registry.get("knowledge.search")

    assert tool is not None
    assert tool.destructive is False
    assert tool_min_role("knowledge.search") == ROLE_MEMBER


def test_keyword_search_ranks_relevant_entry_first(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setattr(kb_store, "_kb_root", lambda _user_id: tmp.name)

    os.makedirs(os.path.join(tmp.name, "topics"), exist_ok=True)
    with open(os.path.join(tmp.name, "topics", "vector.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\n"
            "title: 知识库召回\n"
            "triggers: 语义, 知识库, 召回\n"
            "summary: 先召回，再筛选有效思想。\n"
            "---\n\n"
            "这是一条关于语义召回的有效思想。\n"
        )
    with open(os.path.join(tmp.name, "topics", "weather.md"), "w", encoding="utf-8") as fh:
        fh.write(
            "---\n"
            "title: 天气提醒\n"
            "triggers: 天气, 提醒\n"
            "summary: 和知识库召回无关。\n"
            "---\n\n"
            "天气内容。\n"
        )

    items = kb_store.keyword_search_knowledge(
        user_id=1, query="语义 召回 有效思想", k=2, include_body=True
    )

    assert items, "expected keyword search to return results"
    assert items[0]["file_path"] == "topics/vector.md"
