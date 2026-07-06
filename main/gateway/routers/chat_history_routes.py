"""``/api/chat`` history routes: fetch chat message history, list/create/update/
delete chat sessions, and report per-session total token usage."""

IS_ROUTER_ENTRY = False

import logging
import time
from typing import Dict, Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import defer
from sqlmodel import Session, select

from api.database import get_session
from api.models import ChatMessage, ChatMessageCreate, ChatSession
from .auth import get_current_user
from .chat_base import router
from api.services.chat.chat_persistence import _rebuild_usage_snapshots, _save_message
from api.services.chat.chat_media import delete_message_media
from api.chat_runtime.chat_prompt_utils import _build_mcp_display_result, _safe_json
from api.chat_runtime.chat_runtime_helpers import _live_pending_tokens_for, build_effective_system_prompt

logger = logging.getLogger(__name__)


@router.get("/system-prompt-preview")
def get_system_prompt_preview(
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session_id: Optional[str] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)

    # Prefer the *actual* system prompt the model last received in this session
    # (persisted on the assistant message), so the preview shows ground truth —
    # the exact prompt the AI got, including the dynamic MCP catalog as it was
    # resolved at run time (e.g. which browser/desktop agents were online). This
    # avoids a misleading live re-derivation that can diverge from what the AI saw.
    if session_id:
        last_stmt = select(ChatMessage).where(
            ChatMessage.user_id == user.id,
            ChatMessage.session_id == session_id,
            ChatMessage.ai_kind == ai_kind,
            ChatMessage.role == "assistant",
            ChatMessage.system_prompt.is_not(None),
        ).order_by(ChatMessage.created_at.desc())
        if ai_config_id is not None:
            last_stmt = last_stmt.where(ChatMessage.ai_config_id == ai_config_id)
        last_msg = session.exec(last_stmt).first()
        if last_msg and (last_msg.system_prompt or "").strip():
            return {"prompt": last_msg.system_prompt, "prompt_source": "last_run"}

    # No prior run in this session yet → live-build a best-effort preview using
    # the same single-source-of-truth assembly the inference loop uses.
    prompt = build_effective_system_prompt(
        session,
        user,
        ai_kind=ai_kind,
        ai_config_id=ai_config_id,
        session_id=session_id,
    )
    return {"prompt": prompt, "prompt_source": "runtime_preview"}

# Columns shipped to the chat UI per history message. Deliberately excludes the
# heavy ``system_prompt`` column (the full MCP catalog, tens of KB per assistant
# message): the conversation render never needs it, and the prompt panel reads the
# last-run prompt from the dedicated ``/system-prompt-preview`` endpoint instead.
# Keeping it out of every history row is the single biggest history-load win.
def _history_row_to_dict(msg: ChatMessage) -> dict:
    return {
        "id": msg.id,
        "user_id": msg.user_id,
        "ai_config_id": msg.ai_config_id,
        "ai_kind": msg.ai_kind,
        "session_id": msg.session_id,
        "session_name": msg.session_name,
        "role": msg.role,
        "content": msg.content,
        "think": msg.think,
        "tags": msg.tags,
        "model": msg.model,
        "prompt_tokens": msg.prompt_tokens,
        "completion_tokens": msg.completion_tokens,
        "total_tokens": msg.total_tokens,
        "cache_read_tokens": msg.cache_read_tokens,
        "finish_reason": msg.finish_reason,
        "latency": msg.latency,
        "created_at": msg.created_at,
    }


@router.get("/history")
async def get_chat_history(
    session_id: Optional[str] = "default",
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    after_id: Optional[int] = None,
    before_id: Optional[int] = None,
    limit: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    """Fetch chat history. Three modes:

    * ``after_id`` — incremental new messages produced during a run (ascending,
      unbounded; the count is small).
    * ``before_id`` / ``limit`` — cursor paging for "load older on scroll up":
      returns the newest ``limit`` messages older than ``before_id``.
    * neither + ``limit`` — the latest page (newest ``limit`` messages).
    * neither + no ``limit`` — the full history (legacy/full-snapshot callers).

    Paged results are always returned oldest→newest so the client can render /
    prepend them directly.
    """
    user = get_current_user(authorization, session)
    # ``defer`` keeps the large ``system_prompt`` column from ever being read off
    # disk for these rows; ``_history_row_to_dict`` then drops it from the payload.
    statement = select(ChatMessage).options(defer(ChatMessage.system_prompt)).where(
        ChatMessage.user_id == user.id,
        ChatMessage.session_id == session_id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        statement = statement.where(ChatMessage.ai_config_id == ai_config_id)

    # Incremental tail fetch: ascending, no windowing.
    if after_id is not None:
        statement = statement.where(ChatMessage.id > after_id)
        rows = session.exec(statement.order_by(ChatMessage.created_at.asc())).all()
        return [_history_row_to_dict(row) for row in rows]

    # Newest-first window (optionally older than a cursor), then flip to ascending.
    if before_id is not None:
        statement = statement.where(ChatMessage.id < before_id)
    statement = statement.order_by(ChatMessage.id.desc())
    if limit is not None and limit > 0:
        statement = statement.limit(limit)
    rows = session.exec(statement).all()
    rows.reverse()
    out = [_history_row_to_dict(row) for row in rows]

    # 到达记录起点时：如果整个历史里没有任何 mode.manage 记录（极老的会话），
    # 则在最前面补一条合成记录（id=0，不落库），显示该 AI 当前选中的工作模式。
    # 如果已有切换记录（即使不是第一条），则不再重复注入，避免重复信息。
    # 注意不要按 ai_kind 过滤：主控台里普通 AI 成员的对话是 ai_kind='core'
    # （只有辅助管理员是 'assistant'），模式体系按 ai_config_id 区分即可。
    reached_start = limit is None or int(limit) <= 0 or len(rows) < int(limit)
    if reached_start and ai_config_id is not None:
        has_mode_mcp = any(
            getattr(r, "role", None) == "system"
            and "mcp_tool_call" in (getattr(r, "tags", "") or "")
            and "工具: mode.manage" in (getattr(r, "content", "") or "")
            for r in rows
        )
        if not has_mode_mcp:
            earliest = rows[0] if rows else None
            try:
                out.insert(0, {
                    "id": 0,
                    "user_id": user.id,
                    "ai_config_id": int(ai_config_id),
                    "ai_kind": ai_kind,
                    "session_id": session_id,
                    "session_name": getattr(earliest, "session_name", None) if earliest else None,
                    "role": "system",
                    "content": _initial_mode_bubble_content(
                        _initial_mode_use_result(user.id, int(ai_config_id), backfill=True)
                    ),
                    "think": None,
                    "tags": "mcp_tool_call",
                    "model": None,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": 0,
                    "cache_read_tokens": None,
                    "finish_reason": None,
                    "latency": None,
                    "created_at": (getattr(earliest, "created_at", None) - 1.0) if earliest and hasattr(earliest, "created_at") else time.time(),
                })
            except Exception:
                logger.warning("virtual mode record failed for session %s", session_id, exc_info=True)
    return out

@router.get("/sessions")
def get_sessions(
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    session_stmt = select(ChatSession).where(
        ChatSession.user_id == user.id,
        ChatSession.ai_kind == ai_kind,
    ).order_by(ChatSession.updated_at.desc())
    if ai_config_id is not None:
        session_stmt = session_stmt.where(ChatSession.ai_config_id == ai_config_id)
    results = session.exec(session_stmt).all()

    # Sum tokens per session in the database (GROUP BY) instead of pulling every
    # ChatMessage row (incl. large content/system_prompt text) into Python.
    token_stmt = select(
        ChatMessage.session_id,
        func.coalesce(func.sum(ChatMessage.total_tokens), 0),
    ).where(
        ChatMessage.user_id == user.id,
        ChatMessage.ai_kind == ai_kind,
    ).group_by(ChatMessage.session_id)
    if ai_config_id is not None:
        token_stmt = token_stmt.where(ChatMessage.ai_config_id == ai_config_id)
    token_by_session: Dict[str, int] = {
        (sid or "default"): int(total or 0)
        for sid, total in session.exec(token_stmt).all()
    }

    return [
        {
            "id": row.session_id,
            "name": row.session_name,
            "total_tokens": token_by_session.get(row.session_id, 0),
            "forward_to_bot": bool(getattr(row, "forward_to_bot", False)),
        }
        for row in results
    ]

def _initial_mode_bubble_content(result: dict) -> str:
    """把一次 ``mode.manage use <mode>`` 的结果渲染成标准 ``[MCP工具]`` 气泡文本
    （与推理循环持久化工具调用的格式一致，前端按 mcp_tool_call 原样渲染）。
    使用 result 中的 current_mode_key（若无则回退 initial）。"""
    from api.services.mcp import agent_mode_store as mode_store
    key = str(result.get("current_mode_key") or mode_store.DEFAULT_MODE_KEY)
    arguments = {"action": "use", "mode_key": key}
    result_text = _build_mcp_display_result("mode.manage", {"result": result}, ok=True)
    return (
        "[MCP工具]\n"
        "工具: mode.manage\n"
        "状态: 成功\n\n"
        "[参数]\n"
        f"{_safe_json(arguments)}\n\n"
        "[结果]\n"
        f"{result_text}"
    )


def _initial_mode_use_result(user_id: int, ai_config_id: int, *, backfill: bool = False, mode_key: Optional[str] = None) -> dict:
    """开场/起始记录用的模式说明。
    - 若提供 mode_key 则使用它；否则读取该 AI 当前选中的模式（resolve_current_mode_key）。
    - backfill=True 时仅用于展示，不改变 AI 当前模式。
    - 回退到 initial（DEFAULT_MODE_KEY）。
    """
    from api.services.mcp import agent_mode_store as mode_store

    key = (mode_key or mode_store.resolve_current_mode_key(user_id, ai_config_id) or mode_store.DEFAULT_MODE_KEY)
    if key == "chat":
        key = mode_store.DEFAULT_MODE_KEY
    mode = mode_store.get_mode(user_id, key, ai_config_id)
    if not mode:
        mode_store.ensure_builtin_modes(user_id, ai_config_id)
        mode = mode_store.get_mode(user_id, key, ai_config_id) or {}
    name = mode.get("name") or ("初始对话模式" if key == mode_store.DEFAULT_MODE_KEY else key)
    note_default = (
        f"对话开场处于「{name}」。该模式下"
        + ("只有基础对话工具，无法调用设备端（桌面 / 浏览器 / 安卓）MCP；需要干活时先用 mode.manage(action=use) 切换到对应工作模式。"
           if key == mode_store.DEFAULT_MODE_KEY else
           "可使用该模式对应的工具集。")
    )
    return {
        "success": True,
        "ai_config_id": ai_config_id,
        "current_mode_key": key,
        "name": name,
        "description": mode.get("description", ""),
        "prompt": mode.get("prompt", ""),
        "allow_device_mcp": bool(mode.get("allow_device_mcp", key != mode_store.DEFAULT_MODE_KEY)),
        "note": note_default + ("（历史会话补记，仅作展示，不改变当前模式。）" if backfill else ""),
    }


def _seed_initial_mode_record(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    session_id: str,
    session_name: str,
    ai_kind: str,
) -> None:
    """新会话开场：使用该 AI 当前选中的工作模式（用户上次通过 mode.manage 选择的模式）
    记录一条起始 mode 气泡（role=system + mcp_tool_call，仅 UI 渲染为工具结果）。
    推理层会把匹配的 mode mcp 记录回放为上下文消息 + 兜底合成注入，确保 LLM 收到模式 prompt。
    不会强制切回 initial，从而让用户选择的模式在对话初始显示。
    失败不阻塞会话创建。"""
    from api.services.mcp import agent_mode_store as mode_store

    current_key = mode_store.resolve_current_mode_key(user_id, ai_config_id)
    result = _initial_mode_use_result(user_id, ai_config_id, backfill=False, mode_key=current_key)
    content = _initial_mode_bubble_content(result)
    _save_message(
        session,
        user_id,
        ChatMessageCreate(
            role="system",
            content=content,
            tags="mcp_tool_call",
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            total_tokens=0,
        ),
    )


@router.post("/sessions")
def create_session(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    session_name = req.get("name", "").strip() or "未命名会话"
    ai_config_id = req.get("ai_config_id")
    ai_kind = req.get("ai_kind", "assistant")
    sid = f"session_{int(time.time() * 1000)}"
    row = ChatSession(
        user_id=user.id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=sid,
        session_name=session_name,
    )
    session.add(row)
    session.commit()
    # 新会话使用该 AI 当前选中的工作模式（由用户在对话弹窗中通过模式切换选择）来显示
    # 开场记录，而不是强制 initial。保留用户选择的模式在对话初始显示。
    # 不按 ai_kind 过滤：普通 AI 成员的对话是 ai_kind='core'，只有辅助管理员
    # 是 'assistant'；模式体系按 ai_config_id 区分。
    if ai_config_id is not None:
        try:
            _seed_initial_mode_record(
                session,
                user_id=user.id,
                ai_config_id=int(ai_config_id),
                session_id=sid,
                session_name=session_name,
                ai_kind=ai_kind,
            )
        except Exception:
            logger.warning("seed mode record failed for session %s", sid, exc_info=True)
    return {"id": sid, "name": session_name}

@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(
        select(ChatMessage).where(
            ChatMessage.user_id == user.id,
            ChatMessage.session_id == session_id,
            ChatMessage.ai_kind == ai_kind,
        )
    ).all()
    if ai_config_id is not None:
        rows = [row for row in rows if row.ai_config_id == ai_config_id]
    delete_message_media(session, rows)
    for row in rows:
        session.delete(row)

    sessions = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.session_id == session_id,
            ChatSession.ai_kind == ai_kind,
        )
    ).all()
    if ai_config_id is not None:
        sessions = [row for row in sessions if row.ai_config_id == ai_config_id]
    for row in sessions:
        session.delete(row)

    session.commit()
    _rebuild_usage_snapshots(session, user.id, ai_kind, ai_config_id)
    return {"success": True, "deleted_messages": len(rows)}

@router.put("/sessions/{session_id}")
def rename_session(
    session_id: str,
    req: dict,
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    session_name = str(req.get("name", "")).strip()
    if not session_name:
        raise HTTPException(status_code=400, detail="Session name is required")

    rows = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.session_id == session_id,
            ChatSession.ai_kind == ai_kind,
        )
    ).all()
    if ai_config_id is not None:
        rows = [row for row in rows if row.ai_config_id == ai_config_id]
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    for row in rows:
        row.session_name = session_name
        row.updated_at = time.time()
        session.add(row)

    msg_statement = select(ChatMessage).where(
        ChatMessage.user_id == user.id,
        ChatMessage.session_id == session_id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        msg_statement = msg_statement.where(ChatMessage.ai_config_id == ai_config_id)
    messages = session.exec(msg_statement).all()
    for msg in messages:
        msg.session_name = session_name
        session.add(msg)

    session.commit()
    return {"id": session_id, "name": session_name}

@router.put("/sessions/{session_id}/forward-to-bot")
def set_session_forward_to_bot(
    session_id: str,
    req: dict,
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Toggle whether this conversation forwards assistant replies to the bot."""
    user = get_current_user(authorization, session)
    enabled = bool(req.get("enabled"))

    rows = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.session_id == session_id,
            ChatSession.ai_kind == ai_kind,
        )
    ).all()
    if ai_config_id is not None:
        rows = [row for row in rows if row.ai_config_id == ai_config_id]
    if not rows:
        raise HTTPException(status_code=404, detail="Session not found")

    for row in rows:
        row.forward_to_bot = enabled
        row.updated_at = time.time()
        session.add(row)
    session.commit()

    result = {"id": session_id, "forward_to_bot": enabled}
    if enabled:
        warning = _forward_readiness_warning(session, user.id, ai_config_id)
        if warning:
            result["warning"] = warning
    return result


def _forward_readiness_warning(session: Session, user_id: int, ai_config_id: Optional[int]) -> Optional[str]:
    """Explain (for the UI) why turning on forwarding won't actually deliver."""
    if not ai_config_id:
        return "当前对话未绑定具体 AI（默认助手），无法通过机器人转发"
    from api.models import AssistantAIConfig

    cfg = session.get(AssistantAIConfig, int(ai_config_id))
    if cfg is None or cfg.user_id != user_id:
        return None
    try:
        from connector_runtime.bots.notify import forward_readiness

        return forward_readiness(cfg)
    except Exception:
        return None

@router.get("/total-tokens")
async def get_total_tokens(
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    # Aggregate in SQL rather than materializing every ChatMessage row.
    agg_stmt = select(
        func.coalesce(func.sum(ChatMessage.prompt_tokens), 0),
        func.coalesce(func.sum(ChatMessage.completion_tokens), 0),
        func.coalesce(func.sum(ChatMessage.total_tokens), 0),
        func.count(ChatMessage.id),
    ).where(
        ChatMessage.user_id == user.id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        agg_stmt = agg_stmt.where(ChatMessage.ai_config_id == ai_config_id)
    total_prompt_tokens, total_completion_tokens, total_all_tokens, message_count = (
        session.exec(agg_stmt).one()
    )
    pending = _live_pending_tokens_for(
        session,
        user_id=user.id,
        ai_kind=ai_kind,
        ai_config_id=ai_config_id,
    )

    return {
        "prompt_tokens": int(total_prompt_tokens + pending["prompt_tokens"]),
        "completion_tokens": int(total_completion_tokens + pending["completion_tokens"]),
        "total_tokens": int(total_all_tokens + pending["total_tokens"]),
        "persisted_prompt_tokens": int(total_prompt_tokens),
        "persisted_completion_tokens": int(total_completion_tokens),
        "persisted_total_tokens": int(total_all_tokens),
        "live_prompt_tokens": int(pending["prompt_tokens"]),
        "live_completion_tokens": int(pending["completion_tokens"]),
        "live_total_tokens": int(pending["total_tokens"]),
        "message_count": int(message_count or 0)
    }
