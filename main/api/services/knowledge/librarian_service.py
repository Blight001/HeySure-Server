"""图书管理员（Librarian）业务服务。

职责：
- 接受 AI 员工的"沉淀申请"（propose）→ 落 status=pending
- 接受用户的审批（approve/reject）
- 提供"咨询"（consult）：按 query 在 active 条目中检索
- 提供"主题列表"（list_topics）：渐进披露，只返标题+触发词

文件存储：<workspace_root>/KnowledgeBase/topics/<slug>.md（传承知识）
          以及 topics/**/SKILL.md（传承思想/技能，位于 remote/clawhub、local/manual、local/npx 子目录）
真相源：KnowledgeBase/ 下的 Markdown 文件（直接读取，不依赖 KnowledgeEntry 表）
索引：纯文件驱动 + KnowledgeBase/index.json（文件扫描重建）
注册表：<workspace_root>/KnowledgeBase/index.json（前端可选浏览）

参考：Claude Code Skills 的 progressive disclosure（先标题，再按需读全文）。

此文件现为 re-export 门面 + 公共接口（propose/archive/consult/list_topics/brief/read）。
子模块：
  librarian_core      — 共享常量、路径工具、文件写入、索引、条目解析
  librarian_thoughts  — 传承思想/技能 CRUD + NPX/全局技能
  librarian_builtins  — 内置条目 + 固有属性 + 固有人格 + 系统提示词
  librarian_clawhub   — ClawHub 集成（搜索/安装/更新/删除）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import logging

# ---------- 子模块 re-export ----------

from .librarian_core import (
    # 常量
    _KB_DIR,
    _TOPICS_DIR,
    _ARCHIVE_DIR,
    _INTRINSIC_PROPERTIES_OVERRIDES_FILE,
    _MAX_SUMMARY_LEN,
    _VALID_STATUSES,
    _BUILTIN_UPDATED_AT,
    _BUILTIN_ENTRIES,
    ENDPOINT_KINDS,
    # 路径 / 工具
    _kb_root,
    get_librarian_config_id,
    _slugify,
    _new_memory_id,
    _normalize_triggers,
    _normalize_scope,
    _normalize_endpoint,
    _infer_endpoint_kind,
    _resolve_endpoint_kind,
    _yaml_frontmatter,
    _short_summary,
    _render_procedure_md,
    _safe_write,
    _topic_path,
    _read_text,
    _rebuild_index,
    _split_csv,
    _split_frontmatter,
    _parse_triggers_field,
    # 传承思想（单文件 .md）扫描
    _clawhub_installed_items,
    # 条目
    _load_user_knowledge_entries,
    _entry_dict_from_file_entry,
)

from .librarian_thoughts import (
    _inheritance_thoughts_payload,
    _render_inheritance_thoughts_body,
    list_inheritance_thoughts,
    read_inheritance_thought,
    _text_sha256,
    _line_number,
    _edit_text,
    _apply_one_skill_line_edit,
    _apply_skill_line_edits,
    _has_skill_line_edits,
    edit_inheritance_thought,
    _ensure_skill_frontmatter,
    _extract_skill_triggers,
    _sync_skill_to_knowledge_entry,
    create_inheritance_thought,
    delete_inheritance_thought,
    _SAFE_SKILLS_PACKAGE,
    _normalize_skills_package,
    _global_agent_skills_root,
    _skill_directory_fingerprint,
    _global_skill_snapshot,
    _global_skills_lock_path,
    _global_skills_lock_snapshot,
    _skill_card_metadata,
    _validate_skill_tree_for_import,
    _import_global_skill_snapshot,
    install_npx_skill_package,
)

from .librarian_builtins import (
    _builtin_entries,
    _builtin_entry,
    _INTRINSIC_SCOPE_DESCRIPTIONS,
    _intrinsic_properties_payload,
    _mcp_schema_parameter_rows,
    intrinsic_tool_description,
    intrinsic_input_schema,
    _intrinsic_properties_overrides_path,
    _load_intrinsic_properties_overrides,
    save_intrinsic_properties_overrides,
    _render_intrinsic_properties_body,
    _render_library_mcp_full_body,
    _inheritance_skills_payload,
    _render_inheritance_skills_body,
    _intrinsic_personas_payload,
    _render_intrinsic_personas_body,
    save_intrinsic_persona,
    save_intrinsic_mode_prompt,
    _SYSTEM_PROMPT_SECTIONS,
    _system_prompts_payload,
    save_system_prompts,
    _render_system_prompts_body,
)

from .librarian_clawhub import (
    _SAFE_CLAWHUB_REMOTE_SLUG,
    _SAFE_INSTALLED_SKILL_SLUG,
    _normalize_clawhub_slug,
    search_clawhub_skills,
    clawhub_skill_detail,
    install_clawhub_skill,
    clawhub_installed_skill_detail,
    update_clawhub_installed_skill,
    set_inheritance_thought_endpoint,
    delete_clawhub_installed_skill,
    _latest_clawhub_version,
    _clawhub_trust_summary,
    _raise_if_clawhub_blocked,
    _extract_skill_zip,
)

from ...sio import sio

logger = logging.getLogger(__name__)


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

    librarian_id = get_librarian_config_id(user_id)
    # Pure file-backed entry dict (no KnowledgeEntry table)
    entry_dict = {
        "memory_id": memory_id,
        "title": title,
        "triggers": triggers_norm,
        "scope": scope_norm,
        "scope_target": scope_target_norm,
        "file_path": file_rel,
        "summary": _short_summary(scenario, steps),
        "status": status,
        "confidence": confidence,
        "use_count": 0,
        "last_used_at": None,
        "source_job_id": str(source.get("job_id") or "") or None,
        "source_generation": int(source.get("generation") or 0) or None,
        "source_ai_config_id": int(source.get("ai_config_id") or ai_config_id or 0) or None,
        "source_message_id": int(source.get("message_id") or 0) or None,
        "created_at": now,
        "updated_at": now,
    }

    _rebuild_index(user_id)
    _emit_proposal_event(user_id, "librarian:proposal_resolved", entry_dict)
    return entry_dict


def archive(*, user_id: int, memory_id: str) -> Dict[str, Any]:
    """归档：从 active 移到 archived，文件移到 archives/ 子目录。
    纯文件操作（KnowledgeEntry 表已移除）。
    """
    import os

    # Try file-based locate
    target = None
    try:
        for e in _load_user_knowledge_entries(user_id):
            if e.get("memory_id") == memory_id and e.get("status") == "active":
                target = e
                break
    except Exception:
        target = None

    src_rel = target.get("file_path") if target else None

    if not src_rel:
        raise ValueError("memory not found")

    src = _topic_path(user_id, src_rel)
    bucket = time.strftime("%Y-%m", time.localtime())
    dest_rel = f"{_ARCHIVE_DIR}/{bucket}/{os.path.basename(src_rel)}"
    dest = _topic_path(user_id, dest_rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(src):
            os.replace(src, dest)
    except Exception as exc:
        logger.exception(f"move file failed: {exc}")

    # Pure file operation. Build dict for return + embedding sync.
    now = time.time()
    out = {
        "memory_id": memory_id,
        "file_path": dest_rel,
        "status": "archived",
        "updated_at": now,
    }

    entry_dict = dict(out)
    entry_dict.update({
        "title": memory_id,
        "triggers": [],
        "scope": "global",
        "summary": "",
    })

    try:
        _rebuild_index(user_id)
    except Exception:
        pass
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
    现在直接扫描 KnowledgeBase/ 文件，不再 SELECT KnowledgeEntry 表。
    """
    query_norm = (query or "").strip()
    if not query_norm:
        return []
    q_tokens = _tokenize(query_norm)

    try:
        rows = _load_user_knowledge_entries(user_id)
    except Exception:
        rows = []
    scored: List[tuple[float, Dict[str, Any]]] = []
    for r in rows:
        if r.get("status") != "active":
            continue
        if not _scope_match(r, scope, ai_config_id):
            continue
        score = _score_entry(r, q_tokens, query_norm)
        if score <= 0:
            continue
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("updated_at") or 0)))
    top = scored[: max(1, int(k))]

    # use_count 统计：文件优先模式下可选写回简易 stats（此处简化不强制落盘，保持 0）
    # 如需持久，可在此维护 KnowledgeBase/.knowledge_stats.json
    now = time.time()
    # (no DB mutation; file is source)

    return [_entry_to_dict(r, with_body=True, user_id=user_id) for _, r in top]


def list_topics(
    *,
    user_id: int,
    scope: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """渐进披露：只返标题 + 触发词 + 摘要，不返正文。
    控制台知识库列表只展示系统内置聚合入口；topics/ 下的内容统一归入「传承思想」
    聚合入口查看，不在此逐条作为独立卡片展示。
    """
    target_status = status or "active"
    if target_status not in _VALID_STATUSES and target_status != "all":
        raise ValueError(f"invalid status: {target_status}")
    out: List[Dict[str, Any]] = []
    if target_status in {"active", "all"} and (scope in {None, "", "global"}):
        out.extend(_builtin_entries(user_id=user_id, with_body=False))
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
    现在基于文件扫描（_load_user_knowledge_entries）。
    """
    text_for_match = f"{task_title or ''} {task_instruction or ''}".strip()
    if not text_for_match:
        return ""
    lower = text_for_match.lower()

    try:
        rows = _load_user_knowledge_entries(user_id)
    except Exception:
        rows = []
    scored: List[tuple[float, Dict[str, Any]]] = []
    for r in rows:
        if r.get("status") != "active":
            continue
        if not _scope_match(r, None, ai_config_id):
            continue
        # brief 必须靠"声明式触发词命中"
        triggers = [t.lower() for t in (r.get("triggers") or []) if str(t).strip()]
        trigger_hits = sum(1 for t in triggers if t and t in lower)
        if trigger_hits <= 0:
            continue
        score = trigger_hits * 3.0
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("updated_at") or 0)))
    top = scored[: max(1, int(k))]
    if not top:
        return ""

    lines: List[str] = []
    used = 0
    for _, r in top:
        summary = str(r.get("summary") or "").replace("\n", " ").strip()
        if len(summary) > 200:
            summary = summary[:200] + "…"
        line = f"- 【{r.get('title','')}】({r.get('memory_id','')})：{summary}"
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
    # File-only lookup (KnowledgeEntry table removed)
    try:
        entries = _load_user_knowledge_entries(user_id)
        for e in entries:
            if e.get("memory_id") == memory_id:
                return _entry_to_dict(e, with_body=True, user_id=user_id)
    except Exception as exc:
        logger.info(f"read file scan failed: {exc}")
    raise ValueError("memory not found")


# ---------- 内部工具 ----------

import re as _re
_WORD_PATTERN = _re.compile(r"[一-鿿]|[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return [m.lower() for m in _WORD_PATTERN.findall(text or "")]


def _scope_match(row: Any, scope: Optional[str], ai_config_id: Optional[int]) -> bool:
    # Support both ORM row and plain dict (file-based entries)
    if isinstance(row, dict):
        rscope = str(row.get("scope") or "global")
        rtarget = row.get("scope_target")
    else:
        rscope = str(getattr(row, "scope", "global") or "global")
        rtarget = getattr(row, "scope_target", None)
    if rscope == "global":
        return True
    if scope:
        if scope == "global":
            return rscope == "global"
        if scope == "ai" and rscope == "ai":
            return str(rtarget or "") == str(ai_config_id or "")
        if scope == "project" and rscope == "project":
            return True
    if rscope == "ai" and ai_config_id is not None:
        return str(rtarget or "") == str(ai_config_id)
    return False


def _score_entry(row: Any, q_tokens: List[str], query_text: str) -> float:
    if not q_tokens:
        return 0.0
    if isinstance(row, dict):
        title = row.get("title") or ""
        trigs_raw = row.get("triggers") or ""
        summary = row.get("summary") or ""
    else:
        title = getattr(row, "title", "") or ""
        trigs_raw = getattr(row, "triggers", "") or ""
        summary = getattr(row, "summary", "") or ""
    hay_pieces = [title, trigs_raw, summary]
    hay = " ".join(hay_pieces).lower()
    score = 0.0
    # 触发词命中权重更高
    triggers = _parse_triggers_field(trigs_raw) if not isinstance(trigs_raw, list) else trigs_raw
    triggers = [str(t).lower() for t in triggers if t]
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
    row: Any,
    *,
    with_body: bool = False,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    import os
    if isinstance(row, dict):
        return _entry_dict_from_file_entry(row, with_body=with_body, user_id=user_id)
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
