"""File-based knowledge embeddings (no DB table).

Embeddings (vectors) are stored per-user under:
  server/data/workspace/<user_id>/KnowledgeBase/embeddings/<memory_id>.json

Each file:
{
  "memory_id": "...",
  "content_hash": "...",
  "model": "text-embedding-3-small",
  "dimensions": 1536,
  "vector": [float, ...],
  "updated_at": 1234567890.0
}

This replaces the old KnowledgeEmbedding SQL table.
Search here is semantic (cosine) + optional lexical boost.
The public "knowledge.search" tool currently uses pure keyword file scan,
but librarian.consult / internal paths can use this.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.core.config import user_shared_knowledge_dir
from api.database import engine
from api.http_client import ai_http_post
from api.models import AssistantAIConfig, KnowledgeEntry, User
from api.services.model_presets import resolve_model_preset

try:
    import numpy as _np  # optional accel for cosine
except Exception:  # pragma: no cover
    _np = None

logger = logging.getLogger(__name__)

_EMBEDDINGS_SUBDIR = "embeddings"
_MAX_EMBED_TEXT_CHARS = 12_000
_MAX_EXCERPT_CHARS = 280
_WORD_PATTERN = re.compile(r"[一-鿿]|[A-Za-z0-9]+")


def _kb_root(user_id: int) -> str:
    return user_shared_knowledge_dir(int(user_id))


def _embeddings_dir(user_id: int) -> str:
    root = os.path.join(_kb_root(user_id), _EMBEDDINGS_SUBDIR)
    os.makedirs(root, exist_ok=True)
    return root


def _embedding_path(user_id: int, memory_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(memory_id or ""))
    return os.path.join(_embeddings_dir(user_id), f"{safe}.json")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.info("knowledge_vector read %s failed: %s", path, exc)
        return None


def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not src.startswith("---\n"):
        return {}, src
    end = src.find("\n---\n", 4)
    if end < 0:
        return {}, src
    head = src[4:end]
    body = src[end + 5 :]
    meta: Dict[str, str] = {}
    for line in head.split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.lstrip("\n")


def _topic_path(user_id: int, file_path: str) -> str:
    root = _kb_root(user_id)
    abs_root = os.path.abspath(root)
    joined = os.path.abspath(os.path.join(abs_root, str(file_path or "")))
    try:
        common = os.path.commonpath([abs_root, joined])
    except ValueError:
        common = ""
    if common != abs_root:
        raise ValueError("Access denied: path outside workspace")
    return joined


def _read_topic_body(user_id: int, row_or_meta: Any) -> str:
    """Try to read body from file_path (works with KnowledgeEntry or dict-like)."""
    try:
        fp = getattr(row_or_meta, "file_path", None) or (row_or_meta.get("file_path") if isinstance(row_or_meta, dict) else None)
        if not fp:
            return ""
        path = _topic_path(user_id, fp)
        raw = _read_text(path)
        if raw is None:
            return ""
        _meta, body = _split_frontmatter(raw)
        return body.strip()
    except Exception as exc:
        logger.info("knowledge_vector read_topic_body failed: %s", exc)
        return ""


def _tokenize(text: str) -> List[str]:
    return [m.lower() for m in _WORD_PATTERN.findall(text or "")]


def _score_lexical(row: Any, query_text: str) -> float:
    """Light lexical score on title/triggers/summary (used for hybrid)."""
    q_tokens = _tokenize(query_text)
    if not q_tokens:
        return 0.0
    title = getattr(row, "title", "") or (row.get("title", "") if isinstance(row, dict) else "")
    triggers = getattr(row, "triggers", "") or (row.get("triggers", "") if isinstance(row, dict) else "")
    summary = getattr(row, "summary", "") or (row.get("summary", "") if isinstance(row, dict) else "")
    hay = " ".join([title, triggers, summary]).lower()
    score = 0.0
    trigs = [t.lower() for t in re.split(r"[,，;；\s]+", str(triggers)) if t.strip()]
    for t in trigs:
        if t and t in query_text.lower():
            score += 2.0
    for tk in q_tokens:
        if tk in hay:
            score += 1.0
    return score


# ---------- Embedding credentials & API ----------

def _resolve_embedding_credentials(
    user_id: int,
    ai_config_id: Optional[int],
) -> Tuple[str, str, str, int]:
    model = str(settings.embedding_model or "text-embedding-3-small").strip()
    dims = int(settings.embedding_dimensions or 1536)

    dedicated_key = str(settings.embedding_api_key or "").strip()
    dedicated_url = str(settings.embedding_base_url or "").strip()
    if dedicated_key and dedicated_url:
        return dedicated_key, dedicated_url, model, dims

    with Session(engine) as session:
        user = session.get(User, int(user_id))
        cfg = None
        if ai_config_id is not None:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == int(user_id),
                    AssistantAIConfig.id == int(ai_config_id),
                )
            ).first()
    if user is None:
        return "", "", model, dims

    api_key, base_url, _chat_model = resolve_model_preset(user, cfg)
    if not model:
        model = str(_chat_model or "text-embedding-3-small").strip()
    return api_key, base_url, model, dims


def _embedding_url(base_url: str) -> str:
    text = str(base_url or "").rstrip("/")
    if not text:
        return text
    parts = urlsplit(text)
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")] + "/embeddings"
    elif path.endswith("/responses"):
        path = path[: -len("/responses")] + "/embeddings"
    elif path.endswith("/completions"):
        path = path[: -len("/completions")] + "/embeddings"
    else:
        path = f"{path}/embeddings" if path else "/embeddings"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _embed_text(
    *,
    api_key: str,
    base_url: str,
    model: str,
    dimensions: int,
    text: str,
) -> List[float]:
    if not api_key or not base_url:
        raise ValueError("embedding credentials are not configured")
    txt = (text or "")[:_MAX_EMBED_TEXT_CHARS]
    resp = ai_http_post(
        _embedding_url(base_url),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": model,
            "input": txt,
            "dimensions": dimensions,
        },
        timeout=120,
        stream=False,
    )
    if resp.status_code >= 400:
        raise ValueError(f"embedding request failed HTTP {resp.status_code}: {str(getattr(resp, 'text', '') or '')[:500]}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("embedding response is not an object")
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        vec = first.get("embedding") if isinstance(first, dict) else None
        if isinstance(vec, list):
            out = [float(v) for v in vec if isinstance(v, (int, float))]
            if len(out) > dimensions:
                out = out[:dimensions]
            if len(out) < dimensions:
                out.extend([0.0] * (dimensions - len(out)))
            return out
    raise ValueError("embedding response missing data[0].embedding")


def _content_hash(title: str, triggers: str, summary: str, body: str, model: str, dimensions: int) -> str:
    payload = {
        "title": str(title or ""),
        "triggers": str(triggers or ""),
        "summary": str(summary or ""),
        "body": str(body or "")[:2000],
        "model": model,
        "dimensions": int(dimensions or 0),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------- File-based embedding IO ----------

def load_user_embeddings(user_id: int) -> Dict[str, Dict[str, Any]]:
    """Load all embeddings for the user from KnowledgeBase/embeddings/*.json"""
    out: Dict[str, Dict[str, Any]] = {}
    edir = _embeddings_dir(user_id)
    try:
        for name in os.listdir(edir):
            if not name.endswith(".json"):
                continue
            p = os.path.join(edir, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                mid = str(data.get("memory_id") or "").strip()
                vec = data.get("vector")
                if mid and isinstance(vec, list):
                    out[mid] = {
                        "memory_id": mid,
                        "vector": [float(x) for x in vec if isinstance(x, (int, float))],
                        "model": data.get("model"),
                        "content_hash": data.get("content_hash"),
                        "updated_at": data.get("updated_at"),
                    }
            except Exception as exc:
                logger.info("knowledge_vector load embedding %s failed: %s", name, exc)
    except FileNotFoundError:
        pass
    return out


def write_embedding_file(
    user_id: int,
    memory_id: str,
    vector: List[float],
    *,
    model: str = "",
    dimensions: int = 0,
    content_hash: str = "",
) -> None:
    """Persist embedding vector as JSON file under the user's KnowledgeBase."""
    if not memory_id or not vector:
        return
    data = {
        "memory_id": memory_id,
        "model": model,
        "dimensions": int(dimensions or len(vector)),
        "content_hash": content_hash,
        "vector": [float(v) for v in vector],
        "updated_at": time.time(),
    }
    p = _embedding_path(user_id, memory_id)
    _write_json(p, data)


def remove_embedding_file(user_id: int, memory_id: str) -> None:
    p = _embedding_path(user_id, memory_id)
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ---------- Ensure / compute ----------

def ensure_file_embedding(
    *,
    user_id: int,
    memory_id: str,
    title: str = "",
    triggers: str = "",
    summary: str = "",
    body_text: str = "",
    file_path: str = "",
    ai_config_id: Optional[int] = None,
    force: bool = False,
) -> bool:
    """Compute (if needed) and write embedding file for a topic/skill.

    Returns True if a new/updated vector was written.
    """
    if not memory_id:
        return False

    api_key, base_url, model, dimensions = _resolve_embedding_credentials(user_id, ai_config_id)
    if not api_key or not base_url:
        return False  # no creds configured, skip silently

    if not body_text and file_path:
        try:
            body_text = _read_topic_body(user_id, type("X", (), {"file_path": file_path})())
        except Exception:
            body_text = ""

    h = _content_hash(title, triggers, summary, body_text, model, dimensions)

    existing = None
    p = _embedding_path(user_id, memory_id)
    if os.path.exists(p) and not force:
        try:
            with open(p, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("content_hash") == h:
                return False
        except Exception:
            pass

    text_for_embed = "\n".join(
        x for x in [
            title and f"标题: {title}",
            triggers and f"触发词: {triggers}",
            summary and f"摘要: {summary}",
            body_text and f"正文: {body_text[:8000]}",
        ] if x
    ).strip() or (title or summary or memory_id)

    try:
        vector = _embed_text(
            api_key=api_key,
            base_url=base_url,
            model=model,
            dimensions=dimensions,
            text=text_for_embed,
        )
    except Exception as exc:
        logger.info("knowledge_vector embed failed for %s: %s", memory_id, exc)
        return False

    write_embedding_file(
        user_id,
        memory_id,
        vector,
        model=model,
        dimensions=dimensions,
        content_hash=h,
    )
    return True


# ---------- Semantic search using file embeddings ----------

def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    if _np is not None:
        va = _np.asarray(a, dtype=_np.float32)
        vb = _np.asarray(b, dtype=_np.float32)
        return float(va @ vb / ( _np.linalg.norm(va) * _np.linalg.norm(vb) + 1e-9 ))
    # pure python
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb + 1e-9)


def semantic_search_knowledge(
    *,
    user_id: int,
    query: str,
    k: int = 5,
    scope: Optional[str] = None,
    ai_config_id: Optional[int] = None,
    include_body: bool = False,
    hybrid: bool = True,
) -> List[Dict[str, Any]]:
    """Semantic (vector) search using embeddings stored in the user's KnowledgeBase.

    Falls back gracefully when no embedding creds or no vectors.
    Returns list of result dicts (similar shape to old vector results).
    """
    q = str(query or "").strip()
    if not q:
        return []

    # Try to embed the query
    api_key, base_url, model, dims = _resolve_embedding_credentials(user_id, ai_config_id)
    qvec: Optional[List[float]] = None
    if api_key and base_url:
        try:
            qvec = _embed_text(api_key=api_key, base_url=base_url, model=model, dimensions=dims, text=q)
        except Exception as exc:
            logger.info("knowledge_vector query embed failed: %s", exc)

    if not qvec:
        # No vector possible -> return empty (caller may fall back to keyword)
        return []

    embs = load_user_embeddings(user_id)
    if not embs:
        return []

    # We need metadata (title etc). Prefer loading from KnowledgeEntry table when present,
    # else fall back to parsing the md files.
    candidates: List[Tuple[str, List[float], Dict[str, Any]]] = []
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.user_id == int(user_id),
                    KnowledgeEntry.status == "active",
                )
            ).all()
            for r in rows:
                mid = str(r.memory_id)
                emb = embs.get(mid)
                if not emb or not emb.get("vector"):
                    continue
                meta = {
                    "memory_id": mid,
                    "title": r.title,
                    "triggers": r.triggers,
                    "summary": r.summary,
                    "scope": r.scope,
                    "scope_target": r.scope_target,
                    "file_path": r.file_path,
                    "updated_at": r.updated_at or 0,
                    "use_count": r.use_count or 0,
                }
                candidates.append((mid, emb["vector"], meta))
    except Exception:
        # Fallback: scan files (less metadata)
        # For simplicity in pure-file mode we can skip or implement light parse
        pass

    if not candidates:
        # Try pure file scan for embeddings that have no KnowledgeEntry row yet
        # (rare after sync)
        for mid, emb in embs.items():
            if not emb.get("vector"):
                continue
            candidates.append((mid, emb["vector"], {"memory_id": mid, "updated_at": emb.get("updated_at", 0)}))

    scored = []
    for mid, vec, meta in candidates:
        sem = _cosine(qvec, vec)
        lex = _score_lexical(meta, q) if hybrid else 0.0
        total = sem + (min(lex, 8.0) * 0.12 if hybrid else 0.0)
        scored.append((total, sem, meta))

    scored.sort(key=lambda x: (-x[0], -float(x[2].get("updated_at", 0))))
    top = scored[: max(1, int(k))]

    results: List[Dict[str, Any]] = []
    for total, sem, meta in top:
        body = ""
        if include_body:
            body = _read_topic_body(user_id, meta) or ""
        excerpt = (body or meta.get("summary", "") or "").replace("\n", " ").strip()
        if len(excerpt) > _MAX_EXCERPT_CHARS:
            excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "…"

        item = {
            "memory_id": meta.get("memory_id"),
            "title": meta.get("title", ""),
            "triggers": [t.strip() for t in str(meta.get("triggers") or "").split(",") if t.strip()],
            "summary": meta.get("summary", ""),
            "score": round(float(total), 6),
            "semantic": round(float(sem), 6),
            "file_path": meta.get("file_path", ""),
            "excerpt": excerpt,
        }
        if include_body:
            item["body"] = body
        results.append(item)

    return results


# ---------- Back-compat no-op shims (old names) ----------

def sync_topic_embedding_for_entry(*, user_id: int, row: Any, ai_config_id: Optional[int] = None, force: bool = False) -> None:
    """Compatibility wrapper used by kb_store / librarian."""
    try:
        mid = getattr(row, "memory_id", None) or (row.get("memory_id") if isinstance(row, dict) else None)
        if not mid:
            return
        title = getattr(row, "title", "") or (row.get("title", "") if isinstance(row, dict) else "")
        triggers = getattr(row, "triggers", "") or (row.get("triggers", "") if isinstance(row, dict) else "")
        summary = getattr(row, "summary", "") or (row.get("summary", "") if isinstance(row, dict) else "")
        fp = getattr(row, "file_path", "") or (row.get("file_path", "") if isinstance(row, dict) else "")
        body = _read_topic_body(user_id, row)
        ensure_file_embedding(
            user_id=user_id,
            memory_id=str(mid),
            title=title,
            triggers=triggers,
            summary=summary,
            body_text=body,
            file_path=fp,
            ai_config_id=ai_config_id,
            force=force,
        )
    except Exception as exc:
        logger.info("knowledge_vector sync_topic_embedding_for_entry failed: %s", exc)


def ensure_knowledge_embeddings(**kwargs: Any) -> int:
    """Legacy batch name – no-op or can be implemented by walking topics."""
    return 0


def _knowledge_search_result(**kwargs: Any) -> Dict[str, Any]:
    """Old internal vector path stub. Real semantic search is semantic_search_knowledge."""
    return {"query": "", "count": 0, "items": []}
