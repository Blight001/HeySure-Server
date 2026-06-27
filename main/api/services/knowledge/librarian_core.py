"""librarian_core — 共享常量、路径工具、文件写入、索引、条目解析。

此模块被 librarian_thoughts / librarian_builtins / librarian_clawhub /
librarian_service 导入；本身不导入其他 librarian_* 子模块。
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from sqlmodel import Session, select

from ...database import engine
from ...integrations import clawhub
from ...models import AssistantAIConfig, User
from ...sio import sio
from ...core.config import user_shared_knowledge_dir
from . import kb_store
from .knowledge_vector import sync_topic_embedding_for_entry as _sync_topic_embedding
from mcp_runtime.mcp.core import safe_join
import logging


logger = logging.getLogger(__name__)


_KB_DIR = "KnowledgeBase"
_TOPICS_DIR = "topics"
_ARCHIVE_DIR = "archives"
_INHERITANCE_THOUGHTS_DIR = "inheritance_thoughts"
_CLAWHUB_REMOTE_DIR = "remote/clawhub"
_NPX_SKILLS_DIR = "local/npx"
_MANUAL_SKILLS_DIR = "local/manual"
_INDEX_FILE = "index.json"
_CLAWHUB_SKILLS_STATE_FILE = "clawhub_skills.json"
_INTRINSIC_PROPERTIES_OVERRIDES_FILE = "intrinsic_properties_overrides.json"
_MAX_SUMMARY_LEN = 240
_VALID_STATUSES = {"pending", "active", "archived", "rejected"}
_BUILTIN_UPDATED_AT = 1893456000.0  # 2030-01-01, keeps built-in categories at the top.
_BUILTIN_ENTRIES = {
    "builtin.intrinsic_personas": {
        "title": "固有人格",
        "triggers": ["固有人格", "AI人格", "Prompt"],
        "summary": "当前所有 AI 的人格 prompt 内容。",
    },
    "builtin.system_prompts": {
        "title": "固有思想",
        "triggers": ["固有思想", "提示词配置", "Prompt"],
        "summary": "所有 AI 统一使用的 MCP、任务和通信提示词配置。",
    },
    "builtin.inheritance_skills": {
        "title": "传承技能",
        "triggers": ["传承技能", "固定MCP", "在线MCP", "工坊工具", "MCP工具"],
        "summary": "系统服务端内置 MCP（工具箱 + 图书馆治理）与在线设备实时上报的工具能力。",
    },
    # 纯服务端固定 MCP 视图：与 inheritance_skills 共享同一权威源（注册表 + 文件
    # 覆盖），但只含服务端工具、按 namespace 分组。仅供 read() 解析，不进入
    # _builtin_entries() 列表，因此不会在前端多出一张知识库卡片。
    "builtin.intrinsic_properties": {
        "title": "固有属性",
        "triggers": ["固有属性", "服务端MCP", "系统MCP", "固定MCP"],
        "summary": "系统固定注册的服务端 MCP 工具定义（按 namespace 分组）。",
    },
    "builtin.inheritance_tools": {
        "title": "传承思想",
        "triggers": ["传承思想", "Markdown文件", "思想沉淀"],
        "summary": "从 ClawHub 安装到本地知识库的 Markdown 思想与技能快照。",
    },
}


# ---------- 路径与工具 ----------

def _kb_root(user_id: int) -> str:
    """每用户一份 KB（共享所有 AI）。

    知识库固定挂在用户根目录下（``<user_workspace>/KnowledgeBase``），不随
    各 AI 的独立工作目录切割——图书管理员每用户最多一个，知识对该用户的
    所有 AI 可见。"""
    root = user_shared_knowledge_dir(user_id)
    os.makedirs(root, exist_ok=True)
    return root


def get_librarian_config_id(user_id: int) -> Optional[int]:
    """返回当前 user 的图书管理员 ai_config_id；无则 None。"""
    with Session(engine) as session:
        row = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.is_librarian == True,  # noqa: E712
            )
        ).first()
        return row.id if row else None


def _slugify(title: str) -> str:
    raw = (title or "").strip().lower()
    # 保留中英文与数字
    cleaned = re.sub(r"[^0-9a-z一-鿿]+", "-", raw)
    cleaned = cleaned.strip("-")
    if not cleaned:
        cleaned = "untitled"
    if len(cleaned) > 80:
        cleaned = cleaned[:80]
    return cleaned


def _new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


def _normalize_triggers(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
    elif isinstance(value, str):
        items = [piece.strip() for piece in re.split(r"[,，;；\n]+", value) if piece.strip()]
    else:
        items = []
    seen = set()
    out: List[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:20]


def _normalize_scope(scope: Any, scope_target: Any) -> tuple[str, Optional[str]]:
    raw = str(scope or "global").strip().lower()
    if raw not in {"global", "ai", "project"}:
        raw = "global"
    target = str(scope_target or "").strip() or None
    if raw == "global":
        return "global", None
    return raw, target


# 传承思想端归类：any 通用 / desktop 桌面端 / browser 浏览器端。
ENDPOINT_KINDS = ("any", "desktop", "browser")


def _normalize_endpoint(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"desktop", "windows", "linux", "desktop_windows", "desktop_linux"}:
        return "desktop"
    if raw in {"browser", "extension", "browser_extension", "browser-extension"}:
        return "browser"
    return "any"


def _infer_endpoint_kind(user_id: int, ai_config_id: Optional[int]) -> str:
    """按安装成员当前在线绑定的端侧 agent 类型推断端归类。

    读取共享 ``DevicePresence``（device_type desktop/browser，工坊为
    workshop→归 any）。仅当成员唯一绑定到某一端时返回该端，否则（无绑定 /
    同时绑定多端 / 仅工坊）回 ``any``。best-effort：异常一律回 ``any``。
    """
    try:
        cfg = int(ai_config_id) if ai_config_id else None
    except (TypeError, ValueError):
        cfg = None
    if not cfg:
        return "any"
    try:
        from ...devices.presence import online_devices_for_config

        kinds = {
            _normalize_endpoint(device_type)
            for _device_id, device_type, _caps in online_devices_for_config(user_id, cfg)
        }
    except Exception:
        return "any"
    kinds.discard("any")
    if len(kinds) == 1:
        return next(iter(kinds))
    return "any"


def _resolve_endpoint_kind(user_id: int, ai_config_id: Optional[int], endpoint_kind: Any) -> str:
    """显式传入优先；未传（None/空）则按绑定的端侧 agent 自动推断。"""
    if endpoint_kind is None or str(endpoint_kind).strip() == "":
        return _infer_endpoint_kind(user_id, ai_config_id)
    return _normalize_endpoint(endpoint_kind)


def _yaml_frontmatter(meta: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, list):
            inline = ", ".join(json.dumps(x, ensure_ascii=False) for x in v)
            lines.append(f"{k}: [{inline}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            esc = v.replace("\"", "\\\"")
            lines.append(f"{k}: \"{esc}\"")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _short_summary(scenario: str, steps: List[str]) -> str:
    pieces: List[str] = []
    sc = (scenario or "").strip().replace("\n", " ")
    if sc:
        pieces.append(sc)
    if steps:
        first = (steps[0] or "").strip().replace("\n", " ")
        if first:
            pieces.append(f"步骤 1：{first}")
    text = " · ".join(pieces)
    if len(text) > _MAX_SUMMARY_LEN:
        text = text[:_MAX_SUMMARY_LEN] + "…"
    return text


# ---------- 文件写入 ----------

def _render_procedure_md(
    *,
    memory_id: str,
    title: str,
    triggers: List[str],
    scope: str,
    scope_target: Optional[str],
    scenario: str,
    steps: List[str],
    gotchas: List[str],
    status: str,
    confidence: float,
    source: Dict[str, Any],
    created_at: float,
    updated_at: float,
) -> str:
    fm = _yaml_frontmatter({
        "memory_id": memory_id,
        "title": title,
        "triggers": triggers,
        "scope": scope,
        "scope_target": scope_target,
        "status": status,
        "confidence": confidence,
        "source_job_id": source.get("job_id"),
        "source_generation": source.get("generation"),
        "source_ai_config_id": source.get("ai_config_id"),
        "source_message_id": source.get("message_id"),
        "created_at": created_at,
        "updated_at": updated_at,
    })
    blocks: List[str] = [fm, "", f"# {title}", ""]
    if scenario:
        blocks.append("## 场景 / 触发条件")
        blocks.append("")
        blocks.append(scenario.strip())
        blocks.append("")
    if steps:
        blocks.append("## 操作步骤")
        blocks.append("")
        for i, step in enumerate(steps, 1):
            blocks.append(f"{i}. {step.strip()}")
        blocks.append("")
    if gotchas:
        blocks.append("## 注意事项 / 已知坑")
        blocks.append("")
        for g in gotchas:
            blocks.append(f"- {g.strip()}")
        blocks.append("")
    return "\n".join(blocks)


def _safe_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _topic_path(user_id: int, file_path: str) -> str:
    root = _kb_root(user_id)
    return safe_join(root, file_path)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.info(f"librarian read {path} failed: {exc}")
        return None


# ---------- 索引文件 ----------

def _rebuild_index(user_id: int) -> None:
    """重写 KnowledgeBase/index.json（只含 active+pending，archived/rejected 不进）。
    现在主要从文件扫描重建，DB 作为可选补充。
    """
    try:
        root = _kb_root(user_id)
        items: List[Dict[str, Any]] = []
        # File scan primary
        try:
            file_ents = _load_user_knowledge_entries(user_id)
            for e in file_ents:
                if e.get("status") not in ("active", "pending"):
                    continue
                items.append({
                    "memory_id": e.get("memory_id"),
                    "title": e.get("title"),
                    "triggers": e.get("triggers") or [],
                    "scope": e.get("scope", "global"),
                    "scope_target": e.get("scope_target"),
                    "status": e.get("status"),
                    "confidence": e.get("confidence", 1.0),
                    "file_path": e.get("file_path"),
                    "use_count": e.get("use_count", 0),
                    "summary": e.get("summary"),
                    "updated_at": e.get("updated_at"),
                })
        except Exception:
            pass
        # No DB fallback — pure file scan (KnowledgeEntry table removed)
        path = os.path.join(root, _INDEX_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": items, "updated_at": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.info(f"{exc}")


def _split_csv(value: str) -> List[str]:
    return [piece.strip() for piece in str(value or "").split(",") if piece.strip()]


# ---------- File-driven knowledge entries (KnowledgeBase/ is the only source; KnowledgeEntry table removed) ----------

def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse simple --- key: value frontmatter (supports basic list-like strings)."""
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not src.startswith("---\n"):
        return {}, src
    end = src.find("\n---\n", 4)
    if end < 0:
        return {}, src
    head = src[4:end]
    body = src[end + 5:]
    meta: Dict[str, Any] = {}
    for line in head.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body.lstrip("\n")


def _parse_triggers_field(raw: Any) -> List[str]:
    """Normalize triggers/keywords from frontmatter or legacy to list."""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    s = str(raw or "").strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
        parts = [p.strip().strip("\"'") for p in inner.split(",")]
        return [p for p in parts if p]
    if not s:
        return []
    return [p.strip() for p in re.split(r"[,，;；\n]+", s) if p.strip()]


# ---------- ClawHub 状态工具（被 _load_user_knowledge_entries 及其他模块使用） ----------

def _inheritance_thoughts_root(user_id: int) -> str:
    root = os.path.join(_kb_root(user_id), _INHERITANCE_THOUGHTS_DIR)
    os.makedirs(root, exist_ok=True)
    os.makedirs(safe_join(root, _CLAWHUB_REMOTE_DIR), exist_ok=True)
    return root


def _clawhub_state_path(user_id: int) -> str:
    return os.path.join(_inheritance_thoughts_root(user_id), _CLAWHUB_SKILLS_STATE_FILE)


def _load_clawhub_state(user_id: int) -> Dict[str, Any]:
    try:
        with open(_clawhub_state_path(user_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.info(f"load clawhub state failed: {exc}")
        return {}


def _save_clawhub_state(user_id: int, state: Dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    with open(_clawhub_state_path(user_id), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _clawhub_installed_items(user_id: int) -> List[Dict[str, Any]]:
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    items: List[Dict[str, Any]] = []
    for slug, item in installed.items():
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["slug"] = str(row.get("slug") or slug)
        row["endpoint_kind"] = _normalize_endpoint(row.get("endpoint_kind"))
        rel_path = str(row.get("path") or "").strip()
        row["present"] = bool(rel_path and os.path.isdir(safe_join(_kb_root(user_id), rel_path)))
        items.append(row)
    items.sort(key=lambda item: float(item.get("installed_at") or 0), reverse=True)
    return items


def _load_user_knowledge_entries(user_id: int) -> List[Dict[str, Any]]:
    """Scan KnowledgeBase/topics/*.md and installed SKILL.md to produce entry dicts.

    Pure file scan for listing/reading (KnowledgeEntry table removed).
    File is the source of truth. Scope/status parsed from frontmatter when present.
    """
    entries: List[Dict[str, Any]] = []
    root = _kb_root(user_id)

    # topics/ procedural knowledge
    topics_dir = os.path.join(root, _TOPICS_DIR)
    if os.path.isdir(topics_dir):
        try:
            for fname in os.listdir(topics_dir):
                if not fname.lower().endswith(".md"):
                    continue
                rel = f"{_TOPICS_DIR}/{fname}".replace("\\", "/")
                p = _topic_path(user_id, rel)
                raw = _read_text(p)
                if not raw:
                    continue
                meta, body = _split_frontmatter(raw)
                memory_id = str(meta.get("memory_id") or "").strip()
                if not memory_id:
                    slug = os.path.splitext(fname)[0]
                    memory_id = f"topic:{slug}"
                title = str(meta.get("title") or os.path.splitext(fname)[0])
                triggers = _parse_triggers_field(meta.get("triggers") or meta.get("keywords") or "")
                status = str(meta.get("status") or "active")
                scope = str(meta.get("scope") or "global")
                summary = str(meta.get("summary") or (body or "")[:200]).strip()
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    mtime = time.time()
                entries.append({
                    "memory_id": memory_id,
                    "title": title,
                    "triggers": triggers,
                    "scope": scope,
                    "scope_target": meta.get("scope_target"),
                    "status": status,
                    "confidence": float(meta.get("confidence") or 1.0),
                    "use_count": 0,
                    "last_used_at": None,
                    "file_path": rel,
                    "summary": summary,
                    "source_job_id": meta.get("source_job_id"),
                    "source_generation": meta.get("source_generation"),
                    "source_ai_config_id": meta.get("source_ai_config_id"),
                    "source_message_id": meta.get("source_message_id"),
                    "created_at": float(meta.get("created_at") or mtime),
                    "updated_at": float(meta.get("updated_at") or mtime),
                })
        except Exception as exc:
            logger.info(f"_load_user_knowledge_entries topics failed user={user_id}: {exc}")

    # skills from inheritance_thoughts (via clawhub state)
    try:
        installed = _clawhub_installed_items(user_id)
        for item in installed:
            slug = str(item.get("slug") or "").strip()
            if not slug:
                continue
            rel_path = str(item.get("path") or "").strip().rstrip("/\\")
            if not rel_path:
                continue
            skill_rel = (rel_path + "/SKILL.md").replace("\\", "/")
            skill_abs = _topic_path(user_id, skill_rel)
            if not os.path.exists(skill_abs):
                continue
            memory_id = f"skill:{slug}"
            name = str(item.get("displayName") or slug)
            summary = str(item.get("summary") or item.get("description") or "")
            installed_at = float(item.get("installed_at") or 0)
            entries.append({
                "memory_id": memory_id,
                "title": name,
                "triggers": [],
                "scope": "global",
                "scope_target": None,
                "status": "active",
                "confidence": 1.0,
                "use_count": 0,
                "last_used_at": None,
                "file_path": skill_rel,
                "summary": summary,
                "source_job_id": None,
                "source_generation": None,
                "source_ai_config_id": None,
                "source_message_id": None,
                "created_at": installed_at or time.time(),
                "updated_at": installed_at or time.time(),
            })
    except Exception as exc:
        logger.info(f"_load_user_knowledge_entries skills failed user={user_id}: {exc}")

    return entries


def _entry_dict_from_file_entry(e: Dict[str, Any], *, with_body: bool = False, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Convert internal file entry to API shape (add body if requested)."""
    out = {k: e.get(k) for k in [
        "memory_id", "title", "triggers", "scope", "scope_target", "status",
        "confidence", "use_count", "last_used_at", "file_path", "summary",
        "source_job_id", "source_generation", "source_ai_config_id", "source_message_id",
        "created_at", "updated_at"
    ]}
    if with_body and user_id is not None:
        try:
            fp = e.get("file_path") or ""
            if fp:
                path = _topic_path(user_id, fp)
                raw = _read_text(path)
                if raw is not None:
                    _m, body = _split_frontmatter(raw)
                    out["body"] = body
        except Exception:
            out["body"] = ""
    return out
