"""Cached, deterministic keyword search for KnowledgeBase markdown files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class SearchAdapters:
    read_text: Callable[[str], Optional[str]]
    split_frontmatter: Callable[[str], Tuple[Dict[str, str], str]]
    parse_triggers: Callable[[Any], Any]


@dataclass(frozen=True)
class SearchRequest:
    root: str
    candidates: list[str]
    query: str
    k: int
    include_body: bool
    scope: Optional[str]
    ai_config_id: Optional[int]
    tokenize: Callable[[str], list[str]]
    adapters: SearchAdapters


@dataclass(frozen=True)
class _SearchQuery:
    query_lower: str
    query_tokens: list[str]
    requested_scope: str
    ai_config_id: Optional[int]
    include_body: bool


@lru_cache(maxsize=512)
def _read_search_document(
    path: str,
    mtime_ns: int,
    size: int,
    adapters: SearchAdapters,
) -> Optional[Tuple[Dict[str, str], str]]:
    """Read and parse one document, keyed by its current filesystem version."""
    del mtime_ns, size  # version components are intentionally cache-key only
    raw = adapters.read_text(path)
    if not raw:
        return None
    try:
        return adapters.split_frontmatter(raw)
    except Exception:
        return None


def _scope_matches(
    meta: Dict[str, str],
    requested_scope: str,
    ai_config_id: Optional[int],
) -> bool:
    row_scope = str(meta.get("scope") or "global").strip().lower()
    row_target = str(
        meta.get("scope_target") or meta.get("owner_ai_config_id") or ""
    ).strip()
    if row_scope == "ai" and (
        ai_config_id is None or row_target != str(int(ai_config_id))
    ):
        return False
    return not requested_scope or requested_scope == row_scope


def _title(meta: Dict[str, str], body: str, rel: str) -> str:
    value = str(meta.get("title") or meta.get("name") or "").strip()
    if value:
        return value
    heading = re.search(r"^#\s+(.+)", body, re.M)
    return heading.group(1).strip() if heading else os.path.splitext(os.path.basename(rel))[0]


def _triggers(meta: Dict[str, str], parse_triggers: Callable[[Any], Any]) -> list[str]:
    raw = meta.get("triggers") or meta.get("keywords") or meta.get("tags") or ""
    parsed = parse_triggers(raw)
    if isinstance(parsed, str):
        return [item.strip() for item in re.split(r"[,，;；\n]+", parsed) if item.strip()]
    return [str(item).strip() for item in parsed if str(item).strip()]


def _summary(meta: Dict[str, str], body: str) -> str:
    value = str(meta.get("summary") or meta.get("description") or "").strip()
    return value or (body or "")[:180].replace("\n", " ").strip()


def _score(
    title: str,
    triggers: list[str],
    summary: str,
    body: str,
    query_lower: str,
    query_tokens: list[str],
) -> float:
    haystack = " ".join((title, " ".join(triggers), summary, body)).lower()
    trigger_score = sum(
        3.0 for trigger in triggers if trigger and trigger.lower() in query_lower
    )
    token_score = sum(1.0 for token in query_tokens if token and token in haystack)
    title_score = 2.0 if query_lower in title.lower() else 0.0
    return trigger_score + token_score + title_score


def _item(
    meta: Dict[str, str],
    body: str,
    rel: str,
    score: float,
    triggers: list[str],
    summary: str,
    include_body: bool,
) -> dict[str, Any]:
    excerpt = (body or summary or "").replace("\n", " ").strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:280] + "…"
    result: dict[str, Any] = {
        "memory_id": str(meta.get("memory_id") or meta.get("id") or f"file:{rel}"),
        "title": _title(meta, body, rel),
        "triggers": triggers,
        "summary": summary,
        "score": round(float(score), 6),
        "file_path": rel,
        "excerpt": excerpt,
    }
    if include_body:
        result["body"] = body.strip()
    return result


def _score_file(
    root: str,
    rel: str,
    query: _SearchQuery,
    adapters: SearchAdapters,
) -> Optional[tuple[float, dict[str, Any]]]:
    path = os.path.join(root, rel)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    parsed = _read_search_document(
        path, stat.st_mtime_ns, stat.st_size, adapters
    )
    if parsed is None:
        return None
    meta, body = parsed
    if not _scope_matches(meta, query.requested_scope, query.ai_config_id):
        return None
    title = _title(meta, body, rel)
    triggers = _triggers(meta, adapters.parse_triggers)
    summary = _summary(meta, body)
    score = _score(
        title,
        triggers,
        summary,
        body,
        query.query_lower,
        query.query_tokens,
    )
    if score <= 0:
        return None
    return score, _item(meta, body, rel, score, triggers, summary, query.include_body)


def search_documents(request: SearchRequest) -> list[dict[str, Any]]:
    q = str(request.query or "").strip()
    if not q:
        return []
    query = _SearchQuery(
        query_lower=q.lower(),
        query_tokens=request.tokenize(q),
        requested_scope=str(request.scope or "").strip().lower(),
        ai_config_id=request.ai_config_id,
        include_body=request.include_body,
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for rel in request.candidates:
        try:
            item = _score_file(request.root, rel, query, request.adapters)
        except Exception:
            item = None
        if item is not None:
            scored.append(item)
    scored.sort(key=lambda item: (-item[0], item[1].get("title", "")))
    return [item for _, item in scored[: max(1, int(request.k or 5))]]
