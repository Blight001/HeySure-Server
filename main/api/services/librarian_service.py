"""图书管理员（Librarian）业务服务。

职责：
- 接受 AI 员工的"沉淀申请"（propose）→ 落 status=pending
- 接受用户的审批（approve/reject）
- 提供"咨询"（consult）：按 query 在 active 条目中检索
- 提供"主题列表"（list_topics）：渐进披露，只返标题+触发词

文件存储：<workspace_root>/KnowledgeBase/topics/<slug>.md
索引：KnowledgeEntry 表
注册表：<workspace_root>/KnowledgeBase/index.json（前端可选浏览）

参考：Claude Code Skills 的 progressive disclosure（先标题，再按需读全文）。
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
from typing import Any, Dict, List, Optional

import yaml

from sqlmodel import Session, select

from ..database import engine
from ..integrations import clawhub
from ..models import AssistantAIConfig, KnowledgeEntry, User
from ..sio import sio
from ..core.config import user_shared_knowledge_dir
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
        from ..device_presence import online_devices_for_config

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
    """重写 KnowledgeBase/index.json（只含 active+pending，archived/rejected 不进）。"""
    try:
        root = _kb_root(user_id)
        with Session(engine) as session:
            rows = session.exec(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.user_id == user_id,
                    KnowledgeEntry.status.in_(["active", "pending"]),
                ).order_by(KnowledgeEntry.created_at.desc())
            ).all()
            items = [
                {
                    "memory_id": r.memory_id,
                    "title": r.title,
                    "triggers": _split_csv(r.triggers),
                    "scope": r.scope,
                    "scope_target": r.scope_target,
                    "status": r.status,
                    "confidence": r.confidence,
                    "file_path": r.file_path,
                    "use_count": r.use_count,
                    "summary": r.summary,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]
        path = os.path.join(root, _INDEX_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": items, "updated_at": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.info(f"{exc}")


def _split_csv(value: str) -> List[str]:
    return [piece.strip() for piece in str(value or "").split(",") if piece.strip()]


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


def _inheritance_thoughts_payload(user_id: int) -> Dict[str, Any]:
    installed = _clawhub_installed_items(user_id)
    return {
        "description": "传承思想支持从 ClawHub 或 npx skills 安装 Skill 到本地 KnowledgeBase 快照；运行时只使用本地文件。",
        "registry_url": clawhub.registry_base_url(),
        "storage_root": f"{_INHERITANCE_THOUGHTS_DIR}/{_CLAWHUB_REMOTE_DIR}",
        "installed_total": len(installed),
        "installed": installed,
    }


def list_inheritance_thoughts(*, user_id: int) -> Dict[str, Any]:
    """Return installed inheritance thoughts using their ClawHub slug as ID."""
    payload = _inheritance_thoughts_payload(int(user_id))
    items: List[Dict[str, Any]] = []
    for installed in payload.get("installed") or []:
        item = dict(installed)
        item["id"] = str(item.get("slug") or "")
        items.append(item)
    return {
        "items": items,
        "total": len(items),
        "description": payload.get("description"),
        "storage_root": payload.get("storage_root"),
    }


def read_inheritance_thought(*, user_id: int, thought_id: str) -> Dict[str, Any]:
    """Return one installed inheritance thought by the ID emitted by the list."""
    detail = clawhub_installed_skill_detail(
        user_id=int(user_id),
        slug=str(thought_id or "").strip(),
    )
    detail["id"] = str(detail.get("slug") or thought_id)
    content = str(detail.get("skill_card") or "")
    detail["lines"] = [
        {"line": index, "text": text}
        for index, text in enumerate(content.splitlines(), start=1)
    ]
    detail["line_count"] = len(detail["lines"])
    detail["content_sha256"] = _text_sha256(content)
    return detail


def _text_sha256(content: str) -> str:
    import hashlib

    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _line_number(value: Any, field: str, line_count: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 1 or number > line_count:
        raise ValueError(f"{field} must be between 1 and {line_count}")
    return number


def _edit_text(edit: Dict[str, Any]) -> str:
    if "text" in edit:
        return str(edit.get("text") or "")
    if "content" in edit:
        return str(edit.get("content") or "")
    return ""


def _apply_one_skill_line_edit(lines: List[str], edit: Dict[str, Any]) -> None:
    mode = str(edit.get("mode") or "").strip().lower()
    if not mode and any(key in edit for key in ("line", "line_number", "start_line")):
        mode = "replace_line"
    if mode == "replace_all":
        lines[:] = _edit_text(edit).splitlines()
        return
    if mode in {"append", "prepend"}:
        new_lines = _edit_text(edit).splitlines()
        if mode == "append":
            lines.extend(new_lines)
        else:
            lines[:0] = new_lines
        return
    if not lines:
        raise ValueError(f"{mode or 'line edit'} requires non-empty content")

    if mode in {"replace_line", "delete_line"}:
        start_raw = edit.get("start_line", edit.get("line", edit.get("line_number")))
        if start_raw is None:
            raise ValueError("line/line_number/start_line is required")
        end_raw = edit.get("end_line", start_raw)
        start = _line_number(start_raw, "start_line", len(lines))
        end = _line_number(end_raw, "end_line", len(lines))
        if end < start:
            raise ValueError("end_line must be >= start_line")
        replacement = [] if mode == "delete_line" else _edit_text(edit).splitlines()
        lines[start - 1:end] = replacement
        return
    if mode in {"insert_before", "insert_after"}:
        raw = edit.get("line", edit.get("line_number", edit.get("start_line")))
        if raw is None:
            raise ValueError("line/line_number is required")
        number = _line_number(raw, "line", len(lines))
        index = number - 1 if mode == "insert_before" else number
        lines[index:index] = _edit_text(edit).splitlines()
        return
    raise ValueError(
        "unsupported edit mode; use replace_line, insert_before, insert_after, "
        "delete_line, append, prepend, or replace_all"
    )


def _apply_skill_line_edits(content: str, arguments: Dict[str, Any]) -> tuple[str, int]:
    raw_edits = arguments.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        if not all(isinstance(item, dict) for item in raw_edits):
            raise ValueError("edits must be an array of objects")
        edits = list(raw_edits)
    else:
        edits = [arguments]
    lines = str(content or "").splitlines()
    had_trailing_newline = str(content or "").endswith("\n")
    for edit in edits:
        _apply_one_skill_line_edit(lines, edit)
    updated = "\n".join(lines)
    if had_trailing_newline and updated:
        updated += "\n"
    return updated, len(edits)


def _has_skill_line_edits(arguments: Dict[str, Any]) -> bool:
    """是否带有 SKILL.md 行编辑指令（区别于纯改端）。"""
    raw_edits = arguments.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        return True
    if str(arguments.get("mode") or "").strip():
        return True
    return any(arguments.get(key) is not None for key in ("line", "line_number", "start_line"))


def edit_inheritance_thought(
    *,
    user_id: int,
    thought_id: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """按行编辑 SKILL.md 与/或改端（endpoint_kind）。两者均可单独使用。"""
    thought_id = _normalize_clawhub_slug(thought_id)
    endpoint_raw = arguments.get("endpoint_kind")
    has_endpoint = endpoint_raw is not None and str(endpoint_raw).strip() != ""
    has_line_edits = _has_skill_line_edits(arguments)
    if not has_line_edits and not has_endpoint:
        raise ValueError("nothing to edit: provide line edits and/or endpoint_kind")

    current = clawhub_installed_skill_detail(user_id=int(user_id), slug=thought_id)
    old_content = str(current.get("skill_card") or "")
    old_sha256 = _text_sha256(old_content)
    new_content = old_content
    edit_count = 0

    if has_line_edits:
        expected = str(arguments.get("expected_sha256") or "").strip().lower()
        if expected and expected != old_sha256:
            raise ValueError("SKILL.md changed after it was read; read it again before editing")
        new_content, edit_count = _apply_skill_line_edits(old_content, arguments)
        update_clawhub_installed_skill(
            user_id=int(user_id),
            slug=thought_id,
            skill_card=new_content,
        )

    state = _load_clawhub_state(int(user_id))
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    row = installed.get(thought_id)
    if isinstance(row, dict):
        if has_line_edits:
            install_dir = _clawhub_installed_dir(int(user_id), row)
            metadata = _skill_card_metadata(install_dir, str(row.get("displayName") or thought_id))
            row["displayName"] = metadata["name"]
            row["summary"] = metadata["description"]
        if has_endpoint:
            row["endpoint_kind"] = _normalize_endpoint(endpoint_raw)
        installed[thought_id] = row
        state["installed"] = installed
        _save_clawhub_state(int(user_id), state)

    final_endpoint = _normalize_endpoint(
        row.get("endpoint_kind") if isinstance(row, dict) else endpoint_raw
    )
    return {
        "updated": True,
        "id": thought_id,
        "edit_count": edit_count,
        "endpoint_kind": final_endpoint,
        "old_sha256": old_sha256,
        "content_sha256": _text_sha256(new_content),
        "line_count": len(new_content.splitlines()),
    }


def _ensure_skill_frontmatter(body: str, *, name: str, description: str) -> str:
    """确保 SKILL.md 带 name/description frontmatter，便于元数据解析与运行时使用。"""
    text = str(body or "")
    if text.lstrip().startswith("---"):
        return text if text.endswith("\n") else text + "\n"
    lines = ["---", f"name: {json.dumps(name, ensure_ascii=False)}"]
    if description:
        lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + text.strip() + "\n"


def _extract_skill_triggers(skill_md_text: str, name: str) -> str:
    """从 SKILL.md 提取触发词。
    优先读 frontmatter keywords/tags 字段；
    若没有，以 name 的词作为触发词兜底。
    """
    text = str(skill_md_text or "")
    candidates: List[str] = []
    if text.lstrip().startswith("---"):
        try:
            end = text.find("\n---", 3)
            if end >= 0:
                head = text[3:end]
                try:
                    meta = yaml.safe_load(head)
                    if isinstance(meta, dict):
                        for key in ("keywords", "tags", "triggers", "trigger_words"):
                            val = meta.get(key)
                            if val is not None:
                                candidates.extend(_normalize_triggers(val))
                                break
                except Exception:
                    pass
        except Exception:
            pass
    if not candidates:
        # fallback to name words (split on spaces/punct as word triggers)
        norm_name = re.sub(r'[\s,，;；/\\\-_.]+', ',', str(name or ""))
        candidates = _normalize_triggers(norm_name)
    return ",".join(candidates)


def _sync_skill_to_knowledge_entry(
    user_id: int,
    slug: str,
    name: str,
    summary: str,
    skill_md_path: str,     # 相对 KnowledgeBase/ 的路径
    installed_at: float,
    *,
    ai_config_id: Optional[int] = None,
    status: str = "active",
) -> None:
    """将一个 skill 写入 / 更新 KnowledgeEntry（幂等）。"""
    memory_id = f"skill:{slug}"

    # Sanitize file_path: must be relative path under KnowledgeBase, no leading / or ..
    safe_path = str(skill_md_path or "").strip().lstrip("/\\")
    if ".." in safe_path.replace("\\", "/").split("/"):
        safe_path = ""
    if safe_path and not safe_path.lower().endswith((".md", "skill.md")):
        safe_path = safe_path.rstrip("/\\") + "/SKILL.md"
    skill_md_path = safe_path

    # 读文件提取触发词
    raw = _read_text(_topic_path(user_id, skill_md_path)) if skill_md_path else None
    triggers = _extract_skill_triggers(raw or "", name)
    # upsert KnowledgeEntry
    with Session(engine) as session:
        row = session.exec(
            select(KnowledgeEntry).where(
                KnowledgeEntry.user_id == user_id,
                KnowledgeEntry.memory_id == memory_id,
            )
        ).first()
        now = time.time()
        if row is None:
            row = KnowledgeEntry(
                memory_id=memory_id,
                user_id=user_id,
                title=name,
                triggers=triggers,
                summary=summary,
                file_path=skill_md_path,
                scope="global",
                status=status,
                confidence=1.0,
                created_at=installed_at,
                updated_at=now,
            )
        else:
            row.title = name
            row.triggers = triggers
            row.summary = summary
            row.file_path = skill_md_path
            row.status = status
            row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)

    try:
        _sync_topic_embedding(user_id=user_id, row=row, ai_config_id=ai_config_id, force=True)
    except Exception as exc:
        logger.info("skill file-embedding sync failed slug=%s: %s", slug, exc)

    return {
        "installed": True,
        "slug": slug,
        "entry": _entry_to_dict(row, with_body=False),
    }


def create_inheritance_thought(
    *,
    user_id: int,
    name: str,
    content: str,
    summary: Optional[str] = None,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    """主动创建一条传承思想：AI 直接写 SKILL.md，落本地快照并登记到传承思想库。

    与安装路径（ClawHub/npx）并列，``source="manual"``。``endpoint_kind`` 端归类
    显式优先，未传按安装成员当前绑定的端侧 agent 自动推断。
    """
    import hashlib

    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    body = str(content or "").strip()
    if not body:
        raise ValueError("content is required")

    slug_base = _slugify(name) or "skill"
    suffix = hashlib.sha1(f"{name}-{time.time()}".encode("utf-8")).hexdigest()[:8]
    thought_id = f"manual/{slug_base}-{suffix}"
    safe_name = thought_id.split("/", 1)[1]
    install_rel = f"{_INHERITANCE_THOUGHTS_DIR}/{_MANUAL_SKILLS_DIR}/{safe_name}"
    install_dir = safe_join(_kb_root(user_id), install_rel)
    os.makedirs(install_dir, exist_ok=True)

    summary_text = str(summary or "").strip()
    skill_md = _ensure_skill_frontmatter(body, name=name, description=summary_text)
    _safe_write(os.path.join(install_dir, "SKILL.md"), skill_md)
    installed_at = time.time()
    with open(os.path.join(install_dir, "heysure_manual_create.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"source": "manual", "name": name, "installed_at": installed_at},
            f,
            ensure_ascii=False,
            indent=2,
        )

    meta = _skill_card_metadata(install_dir, name)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    installed[thought_id] = {
        "slug": thought_id,
        "displayName": meta["name"],
        "summary": meta["description"] or summary_text,
        "version": None,
        "ownerHandle": "",
        "source": "manual",
        "path": install_rel,
        "installed_at": installed_at,
        "auto_enabled": False,
        "endpoint_kind": resolved_endpoint,
        "trust": {"verdict": "self-authored"},
    }
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    try:
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=thought_id,
            name=meta["name"],
            summary=meta["description"] or summary_text,
            skill_md_path=install_rel.rstrip("/\\") + "/SKILL.md",
            installed_at=installed_at,
            ai_config_id=ai_config_id,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", thought_id, exc)
    return dict(installed[thought_id], id=thought_id)


def delete_inheritance_thought(*, user_id: int, thought_id: str) -> Dict[str, Any]:
    result = delete_clawhub_installed_skill(
        user_id=int(user_id),
        slug=str(thought_id or "").strip(),
    )
    return {"deleted": True, "id": str(result.get("slug") or thought_id)}


_SAFE_SKILLS_PACKAGE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")


def _normalize_skills_package(package: str) -> str:
    value = str(package or "").strip()
    lowered = value.lower()
    if (
        not value
        or value.startswith(("-", ".", "/", "\\"))
        or re.match(r"^[a-zA-Z]:[\\/]", value)
        or lowered.startswith("file:")
        or not _SAFE_SKILLS_PACKAGE.fullmatch(value)
    ):
        raise ValueError("invalid skills package")
    return value


def _global_agent_skills_root() -> str:
    return os.path.join(str(Path.home()), ".agents", "skills")


def _skill_directory_fingerprint(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    root = os.path.abspath(path)
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs:
            item = os.path.join(current, name)
            if os.path.islink(item):
                rel = os.path.relpath(item, root).replace("\\", "/")
                digest.update(f"link:{rel}\0{os.readlink(item)}".encode("utf-8"))
        for name in files:
            item = os.path.join(current, name)
            if os.path.islink(item):
                rel = os.path.relpath(item, root).replace("\\", "/")
                digest.update(f"link:{rel}\0{os.readlink(item)}".encode("utf-8"))
                continue
            rel = os.path.relpath(item, root).replace("\\", "/")
            digest.update(f"{rel}\0".encode("utf-8"))
            with open(item, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _global_skill_snapshot(root: str) -> Dict[str, str]:
    if not os.path.isdir(root):
        return {}
    snapshot: Dict[str, str] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
            snapshot[name] = _skill_directory_fingerprint(path)
    return snapshot


def _global_skills_lock_path() -> str:
    state_home = str(os.environ.get("XDG_STATE_HOME") or "").strip()
    if state_home:
        return os.path.join(state_home, "skills", ".skill-lock.json")
    return os.path.join(str(Path.home()), ".agents", ".skill-lock.json")


def _global_skills_lock_snapshot() -> Dict[str, str]:
    try:
        with open(_global_skills_lock_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, dict):
        return {}
    return {
        str(name): json.dumps(item, ensure_ascii=False, sort_keys=True)
        for name, item in skills.items()
        if isinstance(item, dict)
    }


def _skill_card_metadata(skill_dir: str, fallback_name: str) -> Dict[str, str]:
    card_path = os.path.join(skill_dir, "SKILL.md")
    with open(card_path, "r", encoding="utf-8") as f:
        card = f.read()
    metadata: Dict[str, Any] = {}
    if card.startswith("---"):
        end = card.find("\n---", 3)
        if end >= 0:
            try:
                loaded = yaml.safe_load(card[3:end])
                metadata = loaded if isinstance(loaded, dict) else {}
            except Exception:
                metadata = {}
    return {
        "name": str(metadata.get("name") or fallback_name).strip() or fallback_name,
        "description": str(metadata.get("description") or "").strip(),
    }


def _validate_skill_tree_for_import(skill_dir: str) -> None:
    if os.path.islink(skill_dir):
        raise ValueError("global skill directory may not be a symlink")
    for current, dirs, files in os.walk(skill_dir, followlinks=False):
        for name in dirs + files:
            if os.path.islink(os.path.join(current, name)):
                raise ValueError(f"skill contains unsupported symlink: {name}")


def _import_global_skill_snapshot(
    *,
    user_id: int,
    package: str,
    skill_name: str,
    source_dir: str,
    endpoint_kind: str = "any",
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    thought_id = f"npx/{skill_name}"
    _validate_skill_tree_for_import(source_dir)
    import hashlib

    safe_name = f"{_slugify(skill_name)}-{hashlib.sha1(skill_name.encode('utf-8')).hexdigest()[:8]}"
    install_rel = f"{_INHERITANCE_THOUGHTS_DIR}/{_NPX_SKILLS_DIR}/{safe_name}"
    install_dir = safe_join(_kb_root(user_id), install_rel)
    temp_dir = f"{install_dir}.tmp-{uuid.uuid4().hex[:8]}"
    os.makedirs(os.path.dirname(install_dir), exist_ok=True)
    try:
        shutil.copytree(source_dir, temp_dir)
        if not os.path.isfile(os.path.join(temp_dir, "SKILL.md")):
            raise ValueError(f"installed skill has no SKILL.md: {skill_name}")
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        os.replace(temp_dir, install_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    meta = _skill_card_metadata(install_dir, skill_name)
    installed_at = time.time()
    install_meta = {
        "source": "npx:skills",
        "package": package,
        "skill_name": skill_name,
        "installed_at": installed_at,
        "global_source_path": source_dir,
    }
    with open(os.path.join(install_dir, "heysure_npx_install.json"), "w", encoding="utf-8") as f:
        json.dump(install_meta, f, ensure_ascii=False, indent=2)

    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    installed[thought_id] = {
        "slug": thought_id,
        "displayName": meta["name"],
        "summary": meta["description"],
        "version": None,
        "ownerHandle": "",
        "source": "npx:skills",
        "package": package,
        "path": install_rel,
        "installed_at": installed_at,
        "auto_enabled": False,
        "endpoint_kind": _normalize_endpoint(endpoint_kind),
        "trust": {"verdict": "unverified"},
    }
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    try:
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=thought_id,
            name=meta["name"],
            summary=meta["description"],
            skill_md_path=install_rel.rstrip("/\\") + "/SKILL.md",
            installed_at=installed_at,
            ai_config_id=ai_config_id,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", thought_id, exc)
    return dict(installed[thought_id], id=thought_id)


def install_npx_skill_package(
    *,
    user_id: int,
    package: str,
    timeout: Optional[int] = None,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Install via skills CLI, then snapshot changed global skills into the KB.

    ``endpoint_kind`` 端归类（any/desktop/browser）：显式传入优先，未传则按安装
    成员当前绑定的端侧 agent 自动推断。
    """
    package = _normalize_skills_package(package)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    try:
        timeout_seconds = int(timeout or 300)
    except (TypeError, ValueError):
        timeout_seconds = 300
    timeout_seconds = max(30, min(timeout_seconds, 600))
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise ValueError("npx is not installed or not available in PATH")

    global_root = _global_agent_skills_root()
    before = _global_skill_snapshot(global_root)
    lock_before = _global_skills_lock_snapshot()
    try:
        result = subprocess.run(
            [npx, "skills", "add", package, "-g", "-y"],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"npx skills install timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise ValueError(f"failed to start npx skills: {exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise ValueError(f"npx skills install failed ({result.returncode}): {output[-4000:]}")

    after = _global_skill_snapshot(global_root)
    lock_after = _global_skills_lock_snapshot()
    changed = [name for name, value in lock_after.items() if lock_before.get(name) != value and name in after]
    if not changed:
        changed = [name for name, fingerprint in after.items() if before.get(name) != fingerprint]
    if not changed:
        raise ValueError("installation succeeded but no new or updated global skills were detected")

    imported = [
        _import_global_skill_snapshot(
            user_id=int(user_id),
            package=package,
            skill_name=name,
            source_dir=os.path.join(global_root, name),
            endpoint_kind=resolved_endpoint,
            ai_config_id=ai_config_id,
        )
        for name in changed
    ]
    return {
        "installed": True,
        "package": package,
        "command": "npx skills add <package> -g -y",
        "imported": imported,
        "total": len(imported),
        "output": output[-8000:],
    }


def _render_inheritance_thoughts_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 传承思想",
        "",
        str(payload.get("description") or ""),
        "",
        f"ClawHub：{payload.get('registry_url') or ''}",
        f"本地目录：KnowledgeBase/{payload.get('storage_root') or ''}",
        f"已安装：{int(payload.get('installed_total') or 0)}",
        "",
    ]
    installed = payload.get("installed") if isinstance(payload.get("installed"), list) else []
    if installed:
        lines.append("## 已安装 ClawHub 技能")
        lines.append("")
        for item in installed:
            slug = str(item.get("slug") or "")
            name = str(item.get("displayName") or slug)
            version = str(item.get("version") or "latest")
            owner = str(item.get("ownerHandle") or "")
            present = "可用" if item.get("present") else "文件缺失"
            lines.append(f"- `{slug}` {name} · {version} · {owner} · {present}")
            summary = str(item.get("summary") or "").strip()
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")
    else:
        lines.append("暂无已安装 ClawHub 技能。")
    return "\n".join(lines).strip()


def _builtin_entries(*, user_id: Optional[int] = None, with_body: bool = False) -> List[Dict[str, Any]]:
    return [
        item
        for memory_id in (
            "builtin.intrinsic_personas",
            "builtin.system_prompts",
            "builtin.inheritance_skills",
            "builtin.inheritance_tools",
        )
        if (item := _builtin_entry(memory_id, user_id=user_id, with_body=with_body)) is not None
    ]


def _builtin_entry(memory_id: str, *, user_id: Optional[int] = None, with_body: bool = False) -> Optional[Dict[str, Any]]:
    meta = _BUILTIN_ENTRIES.get(str(memory_id or ""))
    if not meta:
        return None
    out: Dict[str, Any] = {
        "memory_id": memory_id,
        "title": meta["title"],
        "triggers": list(meta["triggers"]),
        "scope": "global",
        "scope_target": None,
        "status": "active",
        "confidence": 1.0,
        "use_count": 0,
        "last_used_at": None,
        "file_path": "",
        "summary": meta["summary"],
        "source_job_id": None,
        "source_generation": None,
        "source_ai_config_id": None,
        "source_message_id": None,
        "created_at": _BUILTIN_UPDATED_AT,
        "updated_at": _BUILTIN_UPDATED_AT,
    }
    if with_body:
        # 文件为真相源：查看这些内置类目时，确保 KnowledgeBase 目录与文件已生成。
        if user_id and memory_id in ("builtin.intrinsic_personas", "builtin.system_prompts"):
            try:
                kb_store.ensure_user_kb(int(user_id))
            except Exception as exc:
                logger.info(f"ensure_user_kb user={user_id} failed: {exc}")
        if memory_id == "builtin.intrinsic_personas":
            personas = _intrinsic_personas_payload(int(user_id or 0))
            out["intrinsic_personas"] = personas
            out["body"] = _render_intrinsic_personas_body(personas)
        elif memory_id == "builtin.system_prompts":
            prompts = _system_prompts_payload(int(user_id or 0))
            out["system_prompts"] = prompts
            out["body"] = _render_system_prompts_body(prompts)
        elif memory_id == "builtin.intrinsic_properties":
            properties = _intrinsic_properties_payload(int(user_id or 0))
            out["intrinsic_properties"] = properties
            out["body"] = _render_intrinsic_properties_body(properties)
        elif memory_id == "builtin.inheritance_skills":
            skills = _inheritance_skills_payload(int(user_id or 0))
            out["inheritance_skills"] = skills
            out["body"] = _render_inheritance_skills_body(skills)
        elif memory_id == "builtin.inheritance_tools":
            thoughts = _inheritance_thoughts_payload(int(user_id or 0))
            out["inheritance_tools"] = thoughts
            out["body"] = _render_inheritance_thoughts_body(thoughts)
    return out


_INTRINSIC_SCOPE_DESCRIPTIONS = {
    "all": "系统当前固定注册的服务端 MCP 工具定义如下；默认中文展示，编辑后会同步影响 [动态 MCP 说明] 目录与 mcp.describe_tool 的返回。",
    "toolbox": "「工具箱」：每个 AI 默认即可用的系统固定 MCP 工具（无需绑定）；编辑后会同步影响 mcp.describe_tool 的返回。",
    "library": "「图书馆」管理工具：需要该 AI 绑定图书馆后才能调用的治理 / 管理类 MCP（prompt 管理、管理员操作、设备管理、知识库管理）。",
}


def _intrinsic_properties_payload(user_id: int = 0, *, scope: str = "all") -> Dict[str, Any]:
    """服务端固定 MCP 工具视图。``scope``：all 全部 / toolbox 工具箱 / library 图书馆管理工具。"""
    from mcp_runtime.mcp import registry
    from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

    overrides = _load_intrinsic_properties_overrides(user_id) if user_id else {}
    all_tools = sorted(registry.list_tools(), key=lambda item: str(item.get("name") or ""))
    # 文件为真相源：首次把注册表工具导出成 mcp/<ns>/<tool>.md（已存在跳过）。始终按全量
    # seed，避免按 scope 过滤后漏建文件。
    if user_id:
        try:
            kb_store.seed_mcp_tools(
                int(user_id),
                all_tools,
                lambda nm, schema: _mcp_schema_parameter_rows(nm, schema, None),
            )
        except Exception as exc:
            logger.info(f"seed_mcp_tools user={user_id} failed: {exc}")
    if scope == "toolbox":
        tools = [t for t in all_tools if str(t.get("name") or "").strip() not in LIBRARY_BOUND_TOOLS]
    elif scope == "library":
        tools = [t for t in all_tools if str(t.get("name") or "").strip() in LIBRARY_BOUND_TOOLS]
    else:
        tools = all_tools
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        namespace = name.split(".", 1)[0] if "." in name else "other"
        input_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        override = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
        # 参数描述同样文件优先：mcp md 解析出的 params 覆盖旧 override JSON。
        merged_params = dict(override.get("parameters") or {}) if override else {}
        if user_id:
            merged_params.update(kb_store.effective_param_descriptions(int(user_id), name))
        grouped.setdefault(namespace, []).append({
            "name": name,
            "description": intrinsic_tool_description(user_id, name, str(tool.get("description") or "").strip()),
            "inputSchema": intrinsic_input_schema(user_id, name, input_schema),
            "parameters": _mcp_schema_parameter_rows(
                name,
                input_schema,
                merged_params or None,
            ),
            "destructive": bool(tool.get("destructive")),
            "source": "server",
        })

    categories = [
        {
            "namespace": namespace,
            "count": len(items),
            "tools": items,
        }
        for namespace, items in sorted(grouped.items())
    ]
    return {
        "description": _INTRINSIC_SCOPE_DESCRIPTIONS.get(scope, _INTRINSIC_SCOPE_DESCRIPTIONS["all"]),
        "scope": scope,
        "total": len(tools),
        "categories": categories,
    }


def _mcp_schema_parameter_rows(tool_name: str, schema: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required") if isinstance(schema, dict) else []
    required_set = {str(item) for item in required if str(item).strip()} if isinstance(required, list) else set()
    rows: List[Dict[str, Any]] = []
    for name, config in properties.items():
        cfg = config if isinstance(config, dict) else {}
        raw_type = cfg.get("type", "")
        if isinstance(raw_type, list):
            type_name = " | ".join(str(item) for item in raw_type if str(item).strip())
        else:
            type_name = str(raw_type or "").strip()
        param_name = str(name)
        override_description = ""
        if isinstance(overrides, dict):
            override_description = str(overrides.get(param_name) or "").strip()
        rows.append({
            "name": param_name,
            "type": type_name or "any",
            "required": param_name in required_set,
            "description": override_description or str(cfg.get("description") or "").strip(),
        })
    rows.sort(key=lambda item: (not bool(item.get("required")), str(item.get("name") or "")))
    return rows


def intrinsic_tool_description(user_id: int, name: str, raw: str) -> str:
    # 文件为真相源：mcp/<ns>/<tool>.md 优先；其次旧的 override JSON；最后注册表原文。
    if user_id:
        file_desc = kb_store.effective_tool_description(int(user_id), str(name or "").strip(), "")
        if file_desc:
            return file_desc
    override = _load_intrinsic_properties_overrides(user_id).get(str(name or "").strip()) if user_id else {}
    if isinstance(override, dict):
        description = str(override.get("description") or "").strip()
        if description:
            return description
    return str(raw or "").strip()


def intrinsic_input_schema(user_id: int, tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    properties = out.get("properties")
    if not isinstance(properties, dict):
        return out
    override = _load_intrinsic_properties_overrides(user_id).get(str(tool_name or "").strip()) if user_id else {}
    param_overrides = dict(override.get("parameters") or {}) if isinstance(override, dict) else {}
    # 文件为真相源：mcp md 的参数描述覆盖旧 override JSON。
    if user_id:
        param_overrides.update(kb_store.effective_param_descriptions(int(user_id), str(tool_name or "").strip()))
    for name, config in properties.items():
        if not isinstance(config, dict):
            continue
        param_name = str(name)
        override_description = ""
        if isinstance(param_overrides, dict):
            override_description = str(param_overrides.get(param_name) or "").strip()
        config["description"] = override_description or str(config.get("description") or "").strip()
    return out


def _intrinsic_properties_overrides_path(user_id: int) -> str:
    return os.path.join(_kb_root(user_id), _INTRINSIC_PROPERTIES_OVERRIDES_FILE)


def _load_intrinsic_properties_overrides(user_id: int) -> Dict[str, Any]:
    if not user_id:
        return {}
    path = _intrinsic_properties_overrides_path(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tools = data.get("tools") if isinstance(data, dict) else {}
        return tools if isinstance(tools, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.info(f"{exc}")
        return {}


def save_intrinsic_properties_overrides(*, user_id: int, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    from mcp_runtime.mcp import registry

    current = _load_intrinsic_properties_overrides(user_id)
    schema_by_name = {
        str(t.get("name") or "").strip(): (t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {})
        for t in registry.list_tools()
    }
    destructive_by_name = {
        str(t.get("name") or "").strip(): bool(t.get("destructive"))
        for t in registry.list_tools()
    }
    for item in tools or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        parameters_raw = item.get("parameters")
        parameters: Dict[str, str] = {}
        if isinstance(parameters_raw, list):
            for param in parameters_raw:
                if not isinstance(param, dict):
                    continue
                param_name = str(param.get("name") or "").strip()
                if param_name:
                    parameters[param_name] = str(param.get("description") or "").strip()
        elif isinstance(parameters_raw, dict):
            parameters = {
                str(key).strip(): str(value or "").strip()
                for key, value in parameters_raw.items()
                if str(key).strip()
            }
        description = str(item.get("description") or "").strip()
        current[name] = {
            "description": description,
            "parameters": parameters,
        }
        # 文件为真相源：把本次编辑写入 mcp/<ns>/<tool>.md（权威）。
        try:
            param_rows = _mcp_schema_parameter_rows(name, schema_by_name.get(name) or {}, parameters)
            kb_store.write_mcp_tool(int(user_id), name, description, param_rows, destructive_by_name.get(name, False))
        except Exception as exc:
            logger.info(f"write_mcp_tool {name} failed: {exc}")
    # 旧 override JSON 继续保留作为兼容镜像。
    with open(_intrinsic_properties_overrides_path(user_id), "w", encoding="utf-8") as f:
        json.dump({"tools": current, "updated_at": time.time()}, f, ensure_ascii=False, indent=2)
    return _builtin_entry("builtin.intrinsic_properties", user_id=user_id, with_body=True) or {}


def _render_intrinsic_properties_body(payload: Optional[Dict[str, Any]] = None, *, title: str = "固有属性") -> str:
    data = payload or _intrinsic_properties_payload()
    lines = [
        f"# {title}",
        "",
        str(data.get("description") or ""),
        "",
        f"工具总数：{int(data.get('total') or 0)}",
        "",
    ]
    for category in data.get("categories") or []:
        namespace = str(category.get("namespace") or "")
        lines.append(f"## {namespace}")
        lines.append("")
        for tool in category.get("tools") or []:
            name = str(tool.get("name") or "").strip()
            description = str(tool.get("description") or "").strip() or "（无描述）"
            destructive = "（可能产生写入/变更）" if tool.get("destructive") else ""
            lines.append(f"- `{name}`{destructive}: {description}")
            params = tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
            if params:
                for param in params:
                    required = "必填" if param.get("required") else "可选"
                    param_name = str(param.get("name") or "").strip()
                    param_type = str(param.get("type") or "any").strip()
                    param_desc = str(param.get("description") or "").strip() or "（无描述）"
                    lines.append(f"  - 参数 `{param_name}` ({param_type}, {required}): {param_desc}")
            else:
                lines.append("  - 参数：无")
        lines.append("")
    return "\n".join(lines).strip()


def _render_library_mcp_full_body(payload: Optional[Dict[str, Any]] = None) -> str:
    data = payload if isinstance(payload, dict) else {}

    def _append_categories(lines: List[str], section: Dict[str, Any]) -> None:
        for category in section.get("categories") or []:
            namespace = str(category.get("namespace") or "")
            lines.append(f"### {namespace}")
            lines.append("")
            for tool in category.get("tools") or []:
                name = str(tool.get("name") or "").strip()
                description = str(tool.get("description") or "").strip() or "（无描述）"
                destructive = "（可能产生写入/变更）" if tool.get("destructive") else ""
                lines.append(f"- `{name}`{destructive}: {description}")

    lines = [
        "# 图书馆管理工具",
        "",
        str(data.get("description") or ""),
        "",
        f"工具总数：{int(data.get('total') or 0)}",
        "",
    ]
    governance = data.get("governance") if isinstance(data.get("governance"), dict) else {}
    if governance:
        lines.extend([
            "## 治理类工具（AI 配置 mcp_tools 开关）",
            "",
            str(governance.get("description") or "").strip(),
            "",
        ])
        _append_categories(lines, governance)
        lines.append("")
    return "\n".join(lines).strip()


def _inheritance_skills_payload(user_id: int = 0) -> Dict[str, Any]:
    """传承技能 = 工具箱（默认可用服务端工具） + 图书管理工具（治理类，需图书馆绑定） + 当前账号在线端侧实时上报的工具。

    工具箱 与 图书管理工具 作为独立的“设备”出现在传承技能中，不再是单独的知识库条目。
    """
    toolbox_payload = _intrinsic_properties_payload(int(user_id or 0), scope="toolbox")
    library_payload = _intrinsic_properties_payload(int(user_id or 0), scope="library")
    # server_categories 仍用全量（用于服务端工具描述编辑）
    server_payload = _intrinsic_properties_payload(int(user_id or 0))
    server_categories = server_payload.get("categories") if isinstance(server_payload.get("categories"), list) else []

    def _flatten_tools(payload: Dict[str, Any], device_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for category in (payload or {}).get("categories") or []:
            if not isinstance(category, dict):
                continue
            namespace = str(category.get("namespace") or "").strip()
            for spec in category.get("tools") or []:
                if not isinstance(spec, dict):
                    continue
                name = str(spec.get("name") or "").strip()
                if not name:
                    continue
                out.append({
                    **spec,
                    "namespace": namespace,
                    "device_id": device_id,
                    "device_type": "server",
                    "source": "server",
                })
        return out

    toolbox_tools = _flatten_tools(toolbox_payload, "toolbox")
    library_tools = _flatten_tools(library_payload, "library")

    enriched_devices: List[Dict[str, Any]] = [
        {
            "device_id": "toolbox",
            "device_type": "server",
            "updated_at": _BUILTIN_UPDATED_AT,
            "tool_count": len(toolbox_tools),
            "tools": toolbox_tools,
        },
        {
            "device_id": "library",
            "device_type": "server",
            "updated_at": _BUILTIN_UPDATED_AT,
            "tool_count": len(library_tools),
            "tools": library_tools,
        },
    ]
    tools: List[Dict[str, Any]] = list(toolbox_tools) + list(library_tools)

    online_devices: List[Dict[str, Any]] = []
    try:
        from api.device_presence import online_tool_catalog_for_user

        online_devices = online_tool_catalog_for_user(int(user_id or 0))
    except Exception as exc:
        logger.info(f"inheritance skills payload failed: {exc}")
        online_devices = []

    for device in online_devices:
        device_id = str(device.get("device_id") or "")
        device_type = str(device.get("device_type") or "desktop")
        device_tools: List[Dict[str, Any]] = []
        for spec in device.get("tools") or []:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            schema = spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {}
            tool = {
                "name": name,
                "description": str(spec.get("description") or "").strip(),
                "inputSchema": schema,
                "parameters": _mcp_schema_parameter_rows(name, schema, None),
                "destructive": bool(spec.get("destructive")),
                "device_id": device_id,
                "device_type": device_type,
                "implementation": spec.get("implementation") if isinstance(spec.get("implementation"), dict) else {},
            }
            device_tools.append(tool)
            tools.append(tool)
        enriched_devices.append({
            "device_id": device_id,
            "device_type": device_type,
            "updated_at": float(device.get("updated_at") or 0),
            "tool_count": len(device_tools),
            "tools": device_tools,
        })
    return {
        "description": (
            "传承技能包含工具箱（默认可用）、图书管理工具（治理类需绑定图书馆）"
            "以及当前账号在线设备实时上报的工具；服务端工具说明可编辑，保存后同步 mcp.list_tools / mcp.describe_tool。"
        ),
        "workshop": "图书馆（内置）",
        "online": bool(online_devices),
        "server_total": len(toolbox_tools) + len(library_tools),
        "server_categories": server_categories,
        "devices": enriched_devices,
        "device_total": len(enriched_devices),
        "endpoint_device_total": len(online_devices),
        "total": len(tools),
        "tools": tools,
    }


def _render_inheritance_skills_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 传承技能",
        "",
        str(payload.get("description") or ""),
        "",
        f"图书馆：{payload.get('workshop') or ''}",
        f"服务端 MCP 工具数：{int(payload.get('server_total') or 0)}",
        f"设备数：{int(payload.get('device_total') or 0)}（端侧 {int(payload.get('endpoint_device_total') or 0)}）",
        f"传承 MCP 工具总数：{int(payload.get('total') or 0)}",
        "",
    ]
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    if not tools:
        lines.append("暂无在线 MCP 工具。")
        return "\n".join(lines).strip()
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "").strip() or "（无描述）"
        source = f"{tool.get('device_type') or 'device'}:{tool.get('device_id') or ''}"
        lines.append(f"- `{name}` [{source}]: {description}")
        params = tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
        if params:
            for param in params:
                required = "必填" if param.get("required") else "可选"
                param_name = str(param.get("name") or "").strip()
                param_type = str(param.get("type") or "any").strip()
                param_desc = str(param.get("description") or "").strip() or "（无描述）"
                lines.append(f"  - 参数 `{param_name}` ({param_type}, {required}): {param_desc}")
        else:
            lines.append("  - 参数：无")
        implementation = tool.get("implementation") if isinstance(tool.get("implementation"), dict) else {}
        if implementation:
            kind = str(implementation.get("kind") or "unknown")
            lines.append(f"  - 实现类型：`{kind}`")
            source_files = implementation.get("source_files") if isinstance(implementation.get("source_files"), list) else []
            if source_files:
                lines.append("  - 源码入口：" + "、".join(f"`{str(item)}`" for item in source_files if str(item).strip()))
            editable_via = str(implementation.get("editable_via") or "").strip()
            if editable_via:
                lines.append(f"  - 修改入口：`{editable_via}`（先 `inspect`，再 `get_source` / `upsert`）")
            code = implementation.get("code")
            if isinstance(code, list):
                lines.extend(["  - 动态实现：", "", "```json", json.dumps(code, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines).strip()


def _intrinsic_personas_payload(user_id: int) -> Dict[str, Any]:
    with Session(engine) as session:
        rows = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user_id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()

    agents: List[Dict[str, Any]] = []
    for cfg in rows:
        agents.append({
            "id": cfg.id,
            "name": cfg.name,
            "description": cfg.description,
            "role": cfg.ai_role,
            "digital_member_role": cfg.digital_member_role,
            "is_librarian": bool(cfg.is_librarian),
            "enabled": bool(cfg.enabled),
            "model": cfg.model,
            "platform": cfg.platform,
            "generation": cfg.generation,
            "prompt": str(kb_store.effective_ai_prompt(cfg.user_id, cfg) or "").strip(),
            "updated_at": cfg.updated_at,
        })

    return {
        "description": "当前用户下所有 AI 的固定人格 prompt 如下。",
        "total": len(agents),
        "agents": agents,
    }


def _render_intrinsic_personas_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 固有人格",
        "",
        str(payload.get("description") or ""),
        "",
        f"AI 总数：{int(payload.get('total') or 0)}",
        "",
    ]
    for agent in payload.get("agents") or []:
        lines.append(f"## {agent.get('name') or agent.get('id')}")
        lines.append("")
        lines.append(f"- ID：{agent.get('id')}")
        lines.append(f"- 角色：{agent.get('role') or ''}")
        lines.append(f"- 模型：{agent.get('model') or ''}")
        lines.append("")
        lines.append("### 人格 Prompt")
        lines.append("")
        lines.append(str(agent.get("prompt") or "（空）"))
        lines.append("")
    return "\n".join(lines).strip()


def save_intrinsic_persona(
    *,
    user_id: int,
    ai_config_id: int,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """更新一个 AI 的固有人格 Prompt。

    只更新显式给出的字段；文件为真相源，写入 personas/*.md。"""
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == int(ai_config_id),
                AssistantAIConfig.user_id == int(user_id),
            )
        ).first()
        if not cfg:
            raise ValueError("AI config not found")

        effective_prompt = (
            str(prompt) if prompt is not None
            else kb_store.effective_ai_prompt(int(user_id), cfg)
        )
        kb_store.write_persona(int(user_id), cfg, prompt=effective_prompt)
        cfg.updated_at = time.time()
        session.add(cfg)
        session.commit()
        session.refresh(cfg)

    return _builtin_entry("builtin.intrinsic_personas", user_id=user_id, with_body=True) or {}


_SYSTEM_PROMPT_SECTIONS = [
    {
        "key": "mcp",
        "title": "MCP 提示词",
        "items": [
            ("mcp_call_method", "全局 MCP 调用规范", "text"),
            ("mcp_namespace_hints", "MCP namespace 说明（JSON）", "text"),
            ("mcp_dynamic_rule", "MCP 动态工具暴露规则", "text"),
            ("mcp_format_error_hint", "MCP 格式错误提示", "text"),
        ],
    },
    {
        "key": "task",
        "title": "统一任务提示词",
        "items": [
            ("default_start_task_prompt", "任务启动 Prompt", "text"),
            ("default_resume_task_prompt", "任务恢复 Prompt", "text"),
            ("default_supervision_prompt", "监督 Prompt", "text"),
            ("default_supervision_idle_seconds", "AI 停止思考提醒秒数", "number"),
            ("default_compression_prompt", "对话压缩 Prompt", "text"),
            ("task_plan_flow_prompt", "任务分阶段流程 Prompt", "text"),
        ],
    },
    {
        "key": "communication",
        "title": "AI 通信提示词",
        "items": [
            ("prompt_ai_message_notify", "AI 间消息·通知模板", "text"),
            ("prompt_ai_message_inquiry", "AI 间消息·询问模板", "text"),
            ("ai_message_inquiry_reminder_seconds", "询问未回复提醒秒数", "number"),
            ("prompt_ai_message_inquiry_reminder", "AI 间询问未回复提醒模板", "text"),
            ("prompt_ai_message_reply", "AI 间消息·回复模板", "text"),
            ("prompt_ai_message_chitchat", "AI 间消息·闲聊模板", "text"),
            ("prompt_ai_message_reply_success", "AI 间消息回复成功提示", "text"),
            ("prompt_user_message_notice", "用户消息发送提示", "text"),
        ],
    },
]


def _system_prompts_payload(user_id: int) -> Dict[str, Any]:
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            return {"description": "系统设置中的提示词配置。", "total": 0, "sections": []}
        sections: List[Dict[str, Any]] = []
        total = 0
        for section in _SYSTEM_PROMPT_SECTIONS:
            items: List[Dict[str, Any]] = []
            for field, label, value_type in section["items"]:
                # 文本提示词真相源在 system/*.md；数值设置仍读数据库列。
                if value_type == "number":
                    value = getattr(user, field, "")
                else:
                    value = kb_store.effective_system_value(user_id, field, getattr(user, field, None))
                items.append({
                    "key": field,
                    "label": label,
                    "type": value_type,
                    "content": str(value if value is not None else ""),
                })
            total += len(items)
            sections.append({
                "key": section["key"],
                "title": section["title"],
                "count": len(items),
                "items": items,
            })
        return {
            "description": "系统统一提示词如下；任务启动、恢复、监督与传承 Prompt 保存后对所有 AI 生效。",
            "total": total,
            "sections": sections,
        }


def save_system_prompts(*, user_id: int, prompts: List[Dict[str, Any]]) -> Dict[str, Any]:
    allowed: Dict[str, str] = {
        field: value_type
        for section in _SYSTEM_PROMPT_SECTIONS
        for field, _label, value_type in section["items"]
    }
    # 记录文本字段的最终值，提交后据此写文件（不依赖已删列的 getattr）。
    applied_text: Dict[str, str] = {}
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("user not found")
        for item in prompts or []:
            key = str(item.get("key") or "").strip()
            if key not in allowed:
                continue
            raw = item.get("content")
            if allowed[key] == "number":
                # 数值设置项仍存数据库列。
                try:
                    value = int(raw if raw not in {None, ""} else 0)
                except Exception:
                    value = 0
                if key == "default_supervision_idle_seconds":
                    value = max(5, min(3600, value or 25))
                elif key == "ai_message_inquiry_reminder_seconds":
                    value = max(0, min(3600, value))
                setattr(user, key, value)
            elif key == "mcp_namespace_hints":
                raw_text = str(raw or "").strip()
                if raw_text:
                    try:
                        parsed = json.loads(raw_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("mcp_namespace_hints must be a JSON object")
                        raw_text = json.dumps(
                            {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip() and str(v).strip()},
                            ensure_ascii=False,
                        )
                    except Exception:
                        raise ValueError("mcp_namespace_hints must be a JSON object")
                applied_text[key] = raw_text
            elif key == "mcp_call_method":
                text = "\n".join(
                    line for line in str(raw or "").splitlines()
                    if "Call exactly one tool per <mcp-call> block; never join two tool names into one name." not in line
                ).strip()
                applied_text[key] = text
            else:
                applied_text[key] = str(raw or "")
        session.add(user)
        session.commit()
    # 文件为真相源：文本提示词写入 system/<key>.md（权威）。
    for key, value in applied_text.items():
        kb_store.write_system_prompt(int(user_id), key, value)
        if kb_store.read_system_prompt(int(user_id), key) != value.strip():
            raise RuntimeError(f"failed to persist system prompt: {key}")
    return _builtin_entry("builtin.system_prompts", user_id=user_id, with_body=True) or {}


def _render_system_prompts_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 固有思想",
        "",
        str(payload.get("description") or ""),
        "",
        f"配置项总数：{int(payload.get('total') or 0)}",
        "",
    ]
    for section in payload.get("sections") or []:
        lines.append(f"## {section.get('title') or section.get('key')}")
        lines.append("")
        for item in section.get("items") or []:
            lines.append(f"### {item.get('label') or item.get('key')}")
            lines.append("")
            lines.append(str(item.get("content") or "（空）"))
            lines.append("")
    return "\n".join(lines).strip()


# ---------- 传承思想 / ClawHub ----------

_SAFE_CLAWHUB_REMOTE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}$")
# manual/npx 本地快照 slug 允许中文等 Unicode（与 _slugify 一致），ClawHub 远程 slug 仍限 ASCII
_SAFE_INSTALLED_SKILL_SLUG = re.compile(r"^(?:manual|npx)/[^\x00-\x1f\x7f]{1,200}$")


def _normalize_clawhub_slug(slug: str) -> str:
    value = str(slug or "").strip().strip("/")
    if not value or ".." in value.split("/"):
        raise ValueError("invalid ClawHub skill slug")
    if value.startswith("manual/") or value.startswith("npx/"):
        if not _SAFE_INSTALLED_SKILL_SLUG.match(value):
            raise ValueError("invalid installed skill slug")
    elif not _SAFE_CLAWHUB_REMOTE_SLUG.match(value):
        raise ValueError("invalid ClawHub skill slug")
    return value


def search_clawhub_skills(*, user_id: int, query: str, limit: int = 20) -> Dict[str, Any]:
    data = clawhub.search_skills(query, limit=limit, non_suspicious_only=True)
    results = data.get("results") if isinstance(data.get("results"), list) else []
    installed = {item["slug"] for item in _clawhub_installed_items(user_id) if item.get("slug")}
    for item in results:
        if isinstance(item, dict):
            slug = str(item.get("slug") or "")
            item["installed"] = slug in installed
    return {
        "registry_url": clawhub.registry_base_url(),
        "results": results,
        "total": len(results),
    }


def clawhub_skill_detail(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    detail = clawhub.skill_detail(slug)
    version = _latest_clawhub_version(detail)
    skill_card = ""
    scan: Dict[str, Any] = {}
    try:
        skill_card = clawhub.skill_file(slug, "SKILL.md", version=version)
    except Exception as exc:
        skill_card = f"SKILL.md 读取失败：{exc}"
    try:
        scan = clawhub.skill_scan(slug, version=version) if version else clawhub.skill_scan(slug, tag="latest")
    except Exception as exc:
        scan = {"error": str(exc)}
    return {
        "registry_url": clawhub.registry_base_url(),
        "slug": slug,
        "detail": detail,
        "version": version,
        "skill_card": skill_card,
        "scan": scan,
        "installed": any(item.get("slug") == slug for item in _clawhub_installed_items(user_id)),
    }


def install_clawhub_skill(
    *,
    user_id: int,
    slug: str,
    version: Optional[str] = None,
    force: bool = False,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    detail = clawhub.skill_detail(slug)
    resolved_version = str(version or _latest_clawhub_version(detail) or "").strip() or None
    scan: Dict[str, Any] = {}
    try:
        scan = clawhub.skill_scan(slug, version=resolved_version) if resolved_version else clawhub.skill_scan(slug, tag="latest")
    except Exception as exc:
        scan = {"error": str(exc)}
    _raise_if_clawhub_blocked(detail, scan)

    safe_dir_name = _slugify(slug.replace("/", "-").replace("@", "at"))
    install_rel = f"{_INHERITANCE_THOUGHTS_DIR}/{_CLAWHUB_REMOTE_DIR}/{safe_dir_name}"
    install_dir = safe_join(_kb_root(user_id), install_rel)
    if os.path.exists(install_dir):
        if not force:
            raise ValueError("skill already installed; set force=true to update")
        shutil.rmtree(install_dir)
    os.makedirs(install_dir, exist_ok=True)

    blob = clawhub.download_skill_zip(slug, version=resolved_version, tag=None if resolved_version else "latest")
    _extract_skill_zip(blob, install_dir)
    _write_clawhub_install_metadata(install_dir, slug=slug, version=resolved_version, detail=detail, scan=scan)

    skill = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    # 未显式指定端归类时，force 更新保留旧值，否则采用推断结果。
    if endpoint_kind is None or str(endpoint_kind).strip() == "":
        prior = installed.get(slug) if isinstance(installed.get(slug), dict) else {}
        prior_kind = _normalize_endpoint(prior.get("endpoint_kind"))
        effective_endpoint = prior_kind if prior_kind != "any" else resolved_endpoint
    else:
        effective_endpoint = resolved_endpoint
    installed[slug] = {
        "slug": slug,
        "displayName": str(skill.get("displayName") or slug),
        "summary": str(skill.get("summary") or ""),
        "version": resolved_version,
        "ownerHandle": str(owner.get("handle") or skill.get("ownerHandle") or ""),
        "source": "remote:clawhub",
        "path": install_rel,
        "registry_url": clawhub.registry_base_url(),
        "installed_at": time.time(),
        "auto_enabled": False,
        "endpoint_kind": effective_endpoint,
        "trust": _clawhub_trust_summary(detail, scan),
    }
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    skill_entry = installed[slug]
    p = str(skill_entry.get("path") or "").rstrip("/\\")
    try:
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=slug,
            name=str(skill_entry.get("displayName") or slug),
            summary=str(skill_entry.get("summary") or ""),
            skill_md_path=(p + "/SKILL.md") if p else "",
            installed_at=float(skill_entry.get("installed_at") or time.time()),
            ai_config_id=ai_config_id,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", slug, exc)
    return {
        "installed": True,
        "skill": installed[slug],
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def clawhub_installed_skill_detail(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    item = _clawhub_installed_item(user_id, slug)
    install_dir = _clawhub_installed_dir(user_id, item)
    skill_card_path = os.path.join(install_dir, "SKILL.md")
    skill_card = ""
    if os.path.exists(skill_card_path):
        with open(skill_card_path, "r", encoding="utf-8") as f:
            skill_card = f.read()
    metadata_path = os.path.join(install_dir, "heysure_clawhub_install.json")
    if not os.path.exists(metadata_path):
        metadata_path = os.path.join(install_dir, "heysure_npx_install.json")
    metadata: Dict[str, Any] = {}
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            metadata = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            logger.info("read ClawHub install metadata failed: %s", exc)
    return {
        "slug": slug,
        "skill": item,
        "skill_card": skill_card,
        "metadata": metadata,
        "path": item.get("path"),
        "present": os.path.isdir(install_dir),
    }


def update_clawhub_installed_skill(*, user_id: int, slug: str, skill_card: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    item = _clawhub_installed_item(user_id, slug)
    install_dir = _clawhub_installed_dir(user_id, item)
    if not os.path.isdir(install_dir):
        raise ValueError("installed skill files are missing")
    _safe_write(os.path.join(install_dir, "SKILL.md"), str(skill_card or ""))
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    if isinstance(installed.get(slug), dict):
        installed[slug]["edited_at"] = time.time()
        state["installed"] = installed
        _save_clawhub_state(user_id, state)
    try:
        meta = _skill_card_metadata(install_dir, slug)
        rel_path = str(item.get("path") or "").rstrip("/\\")
        skill_md_path = (rel_path + "/SKILL.md") if rel_path else ""
        installed_at = float(item.get("installed_at") or time.time())
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=slug,
            name=meta["name"],
            summary=meta["description"] or "",
            skill_md_path=skill_md_path,
            installed_at=installed_at,
            status="active",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", slug, exc)
    return {
        "updated": True,
        "detail": clawhub_installed_skill_detail(user_id=user_id, slug=slug),
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def set_inheritance_thought_endpoint(*, user_id: int, slug: str, endpoint_kind: Any) -> Dict[str, Any]:
    """改端：更新一条已安装传承思想的端归类（any/desktop/browser）。"""
    slug = _normalize_clawhub_slug(slug)
    kind = _normalize_endpoint(endpoint_kind)
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    if not isinstance(installed.get(slug), dict):
        raise ValueError("installed skill not found")
    installed[slug]["endpoint_kind"] = kind
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    return {
        "updated": True,
        "slug": slug,
        "endpoint_kind": kind,
        "detail": clawhub_installed_skill_detail(user_id=user_id, slug=slug),
    }


def delete_clawhub_installed_skill(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    item = _clawhub_installed_item(user_id, slug)
    install_dir = _clawhub_installed_dir(user_id, item)
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir)
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    installed.pop(slug, None)
    state["installed"] = installed
    _save_clawhub_state(user_id, state)
    try:
        p = str(item.get("path") or "").rstrip("/\\")
        _sync_skill_to_knowledge_entry(
            user_id=user_id,
            slug=slug,
            name=str(item.get("displayName") or slug),
            summary=str(item.get("summary") or ""),
            skill_md_path=(p + "/SKILL.md") if p else "",
            installed_at=float(item.get("installed_at") or time.time()),
            status="archived",
        )
    except Exception as exc:
        logger.warning("skill sync to knowledge entry failed slug=%s: %s", slug, exc)
    return {
        "deleted": True,
        "slug": slug,
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def _clawhub_installed_item(user_id: int, slug: str) -> Dict[str, Any]:
    state = _load_clawhub_state(user_id)
    installed = state.get("installed") if isinstance(state.get("installed"), dict) else {}
    item = installed.get(slug)
    if not isinstance(item, dict):
        raise ValueError("installed skill not found")
    out = dict(item)
    out["endpoint_kind"] = _normalize_endpoint(out.get("endpoint_kind"))
    return out


def _clawhub_installed_dir(user_id: int, item: Dict[str, Any]) -> str:
    rel_path = str(item.get("path") or "").strip()
    if not rel_path:
        raise ValueError("installed skill path is missing")
    return safe_join(_kb_root(user_id), rel_path)


def _latest_clawhub_version(detail: Dict[str, Any]) -> Optional[str]:
    latest = detail.get("latestVersion") if isinstance(detail.get("latestVersion"), dict) else {}
    version = str(latest.get("version") or "").strip()
    if version:
        return version
    skill = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
    tags = skill.get("tags") if isinstance(skill.get("tags"), dict) else {}
    latest_tag = str(tags.get("latest") or "").strip()
    return latest_tag or None


def _clawhub_trust_summary(detail: Dict[str, Any], scan: Dict[str, Any]) -> Dict[str, Any]:
    moderation = detail.get("moderation") if isinstance(detail.get("moderation"), dict) else {}
    scan_moderation = scan.get("moderation") if isinstance(scan.get("moderation"), dict) else {}
    security = scan.get("security") if isinstance(scan.get("security"), dict) else {}
    return {
        "verdict": str(moderation.get("verdict") or security.get("status") or ""),
        "isSuspicious": bool(moderation.get("isSuspicious") or scan_moderation.get("isSuspicious")),
        "isMalwareBlocked": bool(moderation.get("isMalwareBlocked") or scan_moderation.get("isMalwareBlocked")),
        "hasScanResult": bool(security.get("hasScanResult")),
        "blockedFromDownload": bool(security.get("blockedFromDownload")),
        "capabilityTags": security.get("capabilityTags") if isinstance(security.get("capabilityTags"), list) else [],
    }


def _raise_if_clawhub_blocked(detail: Dict[str, Any], scan: Dict[str, Any]) -> None:
    trust = _clawhub_trust_summary(detail, scan)
    verdict = str(trust.get("verdict") or "").lower()
    if trust.get("blockedFromDownload") or trust.get("isMalwareBlocked") or verdict in {"malicious", "blocked"}:
        raise ValueError("ClawHub blocked this skill as unsafe")
    if trust.get("isSuspicious") or verdict == "suspicious":
        raise ValueError("ClawHub marked this skill as suspicious")


def _extract_skill_zip(blob: bytes, dest_dir: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("ClawHub download is not a valid zip") from exc
    dest_abs = os.path.abspath(dest_dir)
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
            raise ValueError(f"unsafe zip path: {info.filename}")
        target = os.path.abspath(os.path.join(dest_abs, name))
        if not target.startswith(dest_abs + os.sep) and target != dest_abs:
            raise ValueError(f"unsafe zip path: {info.filename}")
    archive.extractall(dest_abs)
    if not os.path.exists(os.path.join(dest_abs, "SKILL.md")):
        entries = [name for name in os.listdir(dest_abs) if name != "__MACOSX"]
        if len(entries) == 1:
            wrapped_root = os.path.join(dest_abs, entries[0])
            wrapped_card = os.path.join(wrapped_root, "SKILL.md")
            if os.path.isdir(wrapped_root) and os.path.exists(wrapped_card):
                for child in os.listdir(wrapped_root):
                    shutil.move(os.path.join(wrapped_root, child), os.path.join(dest_abs, child))
                shutil.rmtree(wrapped_root, ignore_errors=True)
        if not os.path.exists(os.path.join(dest_abs, "SKILL.md")):
            logger.info("installed ClawHub skill without root SKILL.md at %s", dest_abs)


def _write_clawhub_install_metadata(dest_dir: str, *, slug: str, version: Optional[str], detail: Dict[str, Any], scan: Dict[str, Any]) -> None:
    metadata = {
        "source": "remote:clawhub",
        "slug": slug,
        "version": version,
        "registry_url": clawhub.registry_base_url(),
        "installed_at": time.time(),
        "detail": detail,
        "scan": scan,
    }
    with open(os.path.join(dest_dir, "heysure_clawhub_install.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


# ---------- 公共接口 ----------

def propose(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    title: str,
    scenario: str,
    steps: List[str],
    gotchas: Optional[List[str]] = None,
    triggers: Optional[List[str]] = None,
    scope: str = "global",
    scope_target: Optional[str] = None,
    source: Optional[Dict[str, Any]] = None,
    auto_approve: bool = True,
) -> Dict[str, Any]:
    """AI 员工调用：沉淀知识。直接写入 status=active，无需用户审批。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    scenario = (scenario or "").strip()
    steps = [s for s in (steps or []) if str(s).strip()]
    if not steps:
        raise ValueError("at least one step is required")
    gotchas = [g for g in (gotchas or []) if str(g).strip()]
    triggers_norm = _normalize_triggers(triggers or [])
    scope_norm, scope_target_norm = _normalize_scope(scope, scope_target)
    source = dict(source or {})

    status = "active"
    confidence = 1.0

    memory_id = _new_memory_id()
    slug = f"{_slugify(title)}-{memory_id[-6:]}"
    file_rel = f"{_TOPICS_DIR}/{slug}.md"
    now = time.time()
    md = _render_procedure_md(
        memory_id=memory_id,
        title=title,
        triggers=triggers_norm,
        scope=scope_norm,
        scope_target=scope_target_norm,
        scenario=scenario,
        steps=steps,
        gotchas=gotchas,
        status=status,
        confidence=confidence,
        source=source,
        created_at=now,
        updated_at=now,
    )
    _safe_write(_topic_path(user_id, file_rel), md)

    with Session(engine) as session:
        librarian_id = get_librarian_config_id(user_id)
        row = KnowledgeEntry(
            memory_id=memory_id,
            user_id=user_id,
            title=title,
            triggers=",".join(triggers_norm),
            scope=scope_norm,
            scope_target=scope_target_norm,
            file_path=file_rel,
            summary=_short_summary(scenario, steps),
            status=status,
            confidence=confidence,
            source_job_id=str(source.get("job_id") or "") or None,
            source_generation=int(source.get("generation") or 0) or None,
            source_ai_config_id=int(source.get("ai_config_id") or ai_config_id or 0) or None,
            source_message_id=int(source.get("message_id") or 0) or None,
            librarian_ai_config_id=librarian_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)

    _rebuild_index(user_id)
    entry_dict = _entry_to_dict(row, with_body=False)
    try:
        _sync_topic_embedding(user_id=user_id, row=row, ai_config_id=librarian_id or None, force=True)
    except Exception as exc:
        logger.info("file-embedding sync after propose failed %s: %s", row.memory_id, exc)
    _emit_proposal_event(user_id, "librarian:proposal_resolved", entry_dict)
    return entry_dict


def archive(*, user_id: int, memory_id: str) -> Dict[str, Any]:
    """归档：从 active 移到 archived，文件移到 archives/ 子目录。"""
    with Session(engine) as session:
        row = session.exec(
            select(KnowledgeEntry).where(
                KnowledgeEntry.user_id == user_id,
                KnowledgeEntry.memory_id == memory_id,
            )
        ).first()
        if not row:
            raise ValueError("memory not found")
        # 移动文件
        src = _topic_path(user_id, row.file_path)
        bucket = time.strftime("%Y-%m", time.localtime())
        dest_rel = f"{_ARCHIVE_DIR}/{bucket}/{os.path.basename(row.file_path)}"
        dest = _topic_path(user_id, dest_rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(src):
                os.replace(src, dest)
        except Exception as exc:
            logger.exception(f"move file failed: {exc}")
        row.status = "archived"
        row.file_path = dest_rel
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        try:
            _sync_topic_embedding(user_id=user_id, row=row, force=True)
        except Exception as exc:
            logger.info("file-embedding sync after archive failed %s: %s", row.memory_id, exc)
        out = _entry_to_dict(row, with_body=False)
    _rebuild_index(user_id)
    return out


def consult(
    *,
    user_id: int,
    query: str,
    scope: Optional[str] = None,
    ai_config_id: Optional[int] = None,
    k: int = 5,
) -> List[Dict[str, Any]]:
    """两阶段检索（P1 无 embedding 版）：

    Stage 1: 触发词与标题的关键词重叠打分
    Stage 2: 在 active + 满足 scope 的条目里取 top-k
    """
    query_norm = (query or "").strip()
    if not query_norm:
        return []
    q_tokens = _tokenize(query_norm)

    with Session(engine) as session:
        rows = session.exec(
            select(KnowledgeEntry).where(
                KnowledgeEntry.user_id == user_id,
                KnowledgeEntry.status == "active",
            )
        ).all()
        scored: List[tuple[float, KnowledgeEntry]] = []
        for r in rows:
            if not _scope_match(r, scope, ai_config_id):
                continue
            score = _score_entry(r, q_tokens, query_norm)
            if score <= 0:
                continue
            scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
        top = scored[: max(1, int(k))]

        # 更新 use_count
        now = time.time()
        for _, r in top:
            r.use_count += 1
            r.last_used_at = now
            session.add(r)
        session.commit()

        return [_entry_to_dict(r, with_body=True, user_id=user_id) for _, r in top]


def list_topics(
    *,
    user_id: int,
    scope: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """渐进披露：只返标题 + 触发词 + 摘要，不返正文。"""
    target_status = status or "active"
    if target_status not in _VALID_STATUSES and target_status != "all":
        raise ValueError(f"invalid status: {target_status}")
    out: List[Dict[str, Any]] = []
    if target_status in {"active", "all"} and (scope in {None, "", "global"}):
        out.extend(_builtin_entries(user_id=user_id, with_body=False))
    with Session(engine) as session:
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.user_id == user_id)
        if target_status != "all":
            stmt = stmt.where(KnowledgeEntry.status == target_status)
        rows = session.exec(stmt.order_by(KnowledgeEntry.updated_at.desc())).all()
        for r in rows:
            if not _scope_match(r, scope, None):
                continue
            out.append({
                "memory_id": r.memory_id,
                "title": r.title,
                "triggers": _split_csv(r.triggers),
                "scope": r.scope,
                "scope_target": r.scope_target,
                "status": r.status,
                "confidence": r.confidence,
                "use_count": r.use_count,
                "summary": r.summary,
                "updated_at": r.updated_at,
            })
        return out


def brief(
    *,
    user_id: int,
    ai_config_id: Optional[int],
    task_title: str,
    task_instruction: str,
    k: int = 5,
    max_chars: int = 1200,
) -> str:
    """生成"任务派发前的预先简报"。

    算法：
    1. 取所有 active 条目；按"任务文本 ↔ 触发词/标题"重叠度排序
    2. 取 top-k；逐条压缩为 "- 【title】(memory_id)：summary"（最长 200 字符）
    3. 总字符上限 max_chars；不超则拼接，超则截尾并加省略
    4. 若全无命中返回空串（不强行注入空 Brief）
    """
    text_for_match = f"{task_title or ''} {task_instruction or ''}".strip()
    if not text_for_match:
        return ""
    lower = text_for_match.lower()

    with Session(engine) as session:
        rows = session.exec(
            select(KnowledgeEntry).where(
                KnowledgeEntry.user_id == user_id,
                KnowledgeEntry.status == "active",
            )
        ).all()
        scored: List[tuple[float, KnowledgeEntry]] = []
        for r in rows:
            if not _scope_match(r, None, ai_config_id):
                continue
            # brief 必须靠"声明式触发词命中"（类 Skills 风格），杜绝标题
            # 子串误命中带来的假阳性；否则 consult 才走更宽的 token 匹配。
            triggers = [t.lower() for t in _split_csv(r.triggers) if t.strip()]
            trigger_hits = sum(1 for t in triggers if t and t in lower)
            if trigger_hits <= 0:
                continue
            score = trigger_hits * 3.0
            scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -x[1].updated_at))
        top = scored[: max(1, int(k))]
        if not top:
            return ""

        lines: List[str] = []
        used = 0
        for _, r in top:
            summary = (r.summary or "").replace("\n", " ").strip()
            if len(summary) > 200:
                summary = summary[:200] + "…"
            line = f"- 【{r.title}】({r.memory_id})：{summary}"
            if used + len(line) + 1 > max_chars:
                lines.append("- …其余条目可在控制台知识库中进一步查询")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def read(
    *,
    user_id: int,
    memory_id: str,
) -> Dict[str, Any]:
    builtin = _builtin_entry(memory_id, user_id=user_id, with_body=True)
    if builtin is not None:
        return builtin
    with Session(engine) as session:
        row = session.exec(
            select(KnowledgeEntry).where(
                KnowledgeEntry.user_id == user_id,
                KnowledgeEntry.memory_id == memory_id,
            )
        ).first()
        if not row:
            raise ValueError("memory not found")
        return _entry_to_dict(row, with_body=True, user_id=user_id)



# ---------- 内部工具 ----------

_WORD_PATTERN = re.compile(r"[一-鿿]|[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return [m.lower() for m in _WORD_PATTERN.findall(text or "")]


def _scope_match(row: KnowledgeEntry, scope: Optional[str], ai_config_id: Optional[int]) -> bool:
    if row.scope == "global":
        return True
    if scope:
        if scope == "global":
            return row.scope == "global"
        if scope == "ai" and row.scope == "ai":
            return str(row.scope_target or "") == str(ai_config_id or "")
        if scope == "project" and row.scope == "project":
            return True  # 项目级先放过，未来加 project_id 匹配
    if row.scope == "ai" and ai_config_id is not None:
        return str(row.scope_target or "") == str(ai_config_id)
    return False


def _score_entry(row: KnowledgeEntry, q_tokens: List[str], query_text: str) -> float:
    if not q_tokens:
        return 0.0
    hay_pieces = [
        row.title or "",
        row.triggers or "",
        row.summary or "",
    ]
    hay = " ".join(hay_pieces).lower()
    score = 0.0
    # 触发词命中权重更高
    triggers = [t.lower() for t in _split_csv(row.triggers)]
    for t in triggers:
        if t and t in query_text.lower():
            score += 2.0
    # 标题/摘要 token 命中
    for tk in q_tokens:
        if tk in hay:
            score += 1.0
    # 长度惩罚极弱，避免噪声
    return score


def _entry_to_dict(
    row: KnowledgeEntry,
    *,
    with_body: bool = False,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "memory_id": row.memory_id,
        "title": row.title,
        "triggers": _split_csv(row.triggers),
        "scope": row.scope,
        "scope_target": row.scope_target,
        "status": row.status,
        "confidence": row.confidence,
        "use_count": row.use_count,
        "last_used_at": row.last_used_at,
        "file_path": row.file_path,
        "summary": row.summary,
        "source_job_id": row.source_job_id,
        "source_generation": row.source_generation,
        "source_ai_config_id": row.source_ai_config_id,
        "source_message_id": row.source_message_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if with_body and user_id is not None:
        try:
            path = _topic_path(user_id, row.file_path)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    out["body"] = f.read()
        except Exception:
            out["body"] = ""
    return out


def _emit_proposal_event(user_id: int, event: str, entry: Dict[str, Any]) -> None:
    """从 sync 上下文向 user 房间广播事件。

    - 若已在事件循环里（如 MCP handler 在异步栈中调用过来）：用
      asyncio.create_task 把 emit 排到当前 loop
    - 若不在事件循环里（如 HTTP 同步路由）：fire-and-forget 一个临时线程
    """
    payload = {
        "userId": user_id,
        "event": event,
        "entry": entry,
        "timestamp": time.time(),
    }
    room = f"user_{user_id}"

    async def _do_emit():
        try:
            await sio.emit(event, payload, room=room)
        except Exception as exc:
            logger.info(f"{event}: {exc}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_emit())
    except RuntimeError:
        import threading

        from api.runtime.async_bridge import run_async

        def _runner() -> None:
            try:
                run_async(_do_emit())
            except Exception as exc:
                logger.info(f"runner: {exc}")

        threading.Thread(target=_runner, daemon=True).start()
