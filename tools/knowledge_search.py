"""Read-only MCP tool for knowledge recall.

Primary: pure keyword scan over files in the user's KnowledgeBase (no DB dependency).
When embedding credentials are configured, it can also leverage file-based
embeddings stored under KnowledgeBase/embeddings/*.json (per-account, no central DB table).
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from api.services import kb_store
from api.services import knowledge_vector


def _knowledge_search(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int] = None):
    query = str((args or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required for knowledge.search")
    try:
        k = int((args or {}).get("k") or 5)
    except Exception:
        k = 5
    include_body = bool((args or {}).get("include_body"))
    scope = (args or {}).get("scope")

    # Try file-based semantic first (vectors live in the user's KnowledgeBase/embeddings/)
    semantic_items: list = []
    try:
        if knowledge_vector and hasattr(knowledge_vector, "semantic_search_knowledge"):
            semantic_items = knowledge_vector.semantic_search_knowledge(
                user_id=int(user_id),
                query=query,
                k=k,
                scope=scope,
                ai_config_id=ai_config_id,
                include_body=include_body,
            ) or []
    except Exception:
        semantic_items = []

    if semantic_items:
        return {
            "query": query,
            "count": len(semantic_items),
            "items": semantic_items,
            "mode": "semantic+file",
        }

    # Fallback to reliable pure keyword file scan (no embedding dependency)
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
            "query": {"type": "string", "description": "查询文本。优先尝试文件-based semantic（如果配置了 embedding 并有向量文件），否则回退到纯关键词文件扫描。"},
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

