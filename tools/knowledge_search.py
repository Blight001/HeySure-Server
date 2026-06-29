"""Read-only MCP tool for knowledge recall.

Pure keyword scan over files in the user's KnowledgeBase (topics/ + skills).
No embeddings / vector store — retrieval is fully file-based and dependency-free.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.services.knowledge import kb_store


def _knowledge_search(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int] = None):
    query = str((args or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required for knowledge.search")
    try:
        k = int((args or {}).get("k") or 5)
    except Exception:
        k = 5
    include_body = bool((args or {}).get("include_body"))

    items = kb_store.keyword_search_knowledge(user_id=int(user_id), query=query, k=k, include_body=include_body)
    return {
        "query": query,
        "count": len(items),
        "items": items,
        "mode": "keyword+file",
    }


def knowledge_search_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询文本。基于关键词对 KnowledgeBase 文件（topics/ 与技能卡）做文件扫描匹配。"},
            "k": {"type": "integer", "description": "返回结果数量，默认 5。"},
            "scope": {
                "type": "string",
                "enum": ["global", "ai", "project"],
                "description": "可选作用域过滤（当前 keyword 模式下忽略）。",
            },
            "include_body": {"type": "boolean", "description": "是否返回全文正文。"},
        },
        "required": ["query"],
    }


KNOWLEDGE_SEARCH_SCHEMA: Dict[str, Any] = knowledge_search_schema()
