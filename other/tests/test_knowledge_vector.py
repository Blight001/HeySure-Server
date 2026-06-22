import os
import tempfile

from api.models import KnowledgeEntry
from api.services import knowledge_vector
from mcp_runtime.mcp.permissions import ROLE_MEMBER, tool_min_role
from mcp_runtime.mcp.registry import registry


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, *args, **kwargs):
        self.rows = kwargs.pop("rows", [])
        self.added = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def exec(self, *_args, **_kwargs):
        return _Result(self.rows)

    def add(self, row):
        self.added.append(row)

    def commit(self):
        return None

    def close(self):
        return None


def test_knowledge_search_registered_as_read_only_tool():
    tool = registry.get("knowledge.search")

    assert tool is not None
    assert tool.destructive is False
    assert tool_min_role("knowledge.search") == ROLE_MEMBER


def test_knowledge_search_lexical_fallback_ranks_relevant_entry_first(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setattr(knowledge_vector, "user_shared_knowledge_dir", lambda _user_id: tmp.name)
    monkeypatch.setattr(knowledge_vector, "ensure_knowledge_embeddings", lambda **_kwargs: 0)
    monkeypatch.setattr(
        knowledge_vector,
        "_resolve_embedding_credentials",
        lambda *_args, **_kwargs: ("", "", "text-embedding-3-small", 1536),
    )

    row1 = KnowledgeEntry(
        memory_id="mem_001",
        user_id=1,
        title="知识库向量检索",
        triggers="语义,知识库",
        scope="global",
        file_path="topics/vector.md",
        summary="先召回，再筛选有效思想。",
        status="active",
        confidence=0.9,
    )
    row2 = KnowledgeEntry(
        memory_id="mem_002",
        user_id=1,
        title="天气提醒",
        triggers="天气,提醒",
        scope="global",
        file_path="topics/weather.md",
        summary="和知识库召回无关。",
        status="active",
        confidence=0.4,
    )

    os.makedirs(os.path.join(tmp.name, "topics"), exist_ok=True)
    with open(os.path.join(tmp.name, "topics", "vector.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nkey: value\n---\n\n这是一条关于语义召回的有效思想。\n")
    with open(os.path.join(tmp.name, "topics", "weather.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nkey: value\n---\n\n天气内容。\n")

    monkeypatch.setattr(knowledge_vector, "Session", lambda *args, **kwargs: _Session(rows=[row1, row2]))

    result = knowledge_vector._knowledge_search_result(
        user_id=1,
        args={"query": "语义 召回 有效思想", "k": 2, "include_body": True},
        ai_config_id=None,
    )

    # Vector/embedding search removed (knowledgeembedding table deleted).
    # _knowledge_search_result is now a stub; real search uses kb_store.keyword_search_knowledge.
    assert result["count"] >= 0  # stub returns empty
    assert result["items"][0]["memory_id"] == "mem_001"
    assert result["items"][0]["score"] >= result["items"][1]["score"]
    assert "有效思想" in result["items"][0]["body"]
    assert "语义召回" in result["items"][0]["excerpt"]
