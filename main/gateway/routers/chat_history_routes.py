"""``/api/chat`` history routes: fetch chat message history, list/create/update/
delete chat sessions, and report per-session total token usage."""

IS_ROUTER_ENTRY = False

import time
from typing import Dict, List, Optional

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from api.database import get_session
from api.models import ChatMessage, ChatSession
from .auth import get_current_user
from .chat_base import router
from api.services.chat_persistence import _rebuild_usage_snapshots
from api.services.chat_media import delete_message_media
from api.chat_runtime.chat_runtime_helpers import _live_pending_tokens_for, build_effective_system_prompt


@router.get("/system-prompt-preview")
async def get_system_prompt_preview(
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

@router.get("/history", response_model=List[ChatMessage])
async def get_chat_history(
    session_id: Optional[str] = "default",
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    after_id: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    statement = select(ChatMessage).where(
        ChatMessage.user_id == user.id,
        ChatMessage.session_id == session_id,
        ChatMessage.ai_kind == ai_kind,
    ).order_by(ChatMessage.created_at.asc())
    if ai_config_id is not None:
        statement = statement.where(ChatMessage.ai_config_id == ai_config_id)
    if after_id is not None:
        statement = statement.where(ChatMessage.id > after_id)
    results = session.exec(statement).all()
    return results

@router.get("/sessions")
async def get_sessions(
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

    msg_stmt = select(ChatMessage).where(
        ChatMessage.user_id == user.id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        msg_stmt = msg_stmt.where(ChatMessage.ai_config_id == ai_config_id)
    messages = session.exec(msg_stmt).all()
    token_by_session: Dict[str, int] = {}
    for msg in messages:
        sid = msg.session_id or "default"
        token_by_session[sid] = token_by_session.get(sid, 0) + int(msg.total_tokens or 0)

    return [
        {
            "id": row.session_id,
            "name": row.session_name,
            "total_tokens": token_by_session.get(row.session_id, 0),
            "forward_to_bot": bool(getattr(row, "forward_to_bot", False)),
        }
        for row in results
    ]

@router.post("/sessions")
async def create_session(
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
    return {"id": sid, "name": session_name}

@router.delete("/sessions/{session_id}")
async def delete_session(
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
async def rename_session(
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
async def set_session_forward_to_bot(
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
    statement = select(ChatMessage).where(
        ChatMessage.user_id == user.id,
        ChatMessage.ai_kind == ai_kind,
    )
    if ai_config_id is not None:
        statement = statement.where(ChatMessage.ai_config_id == ai_config_id)
    messages = session.exec(statement).all()

    total_prompt_tokens = sum(msg.prompt_tokens or 0 for msg in messages)
    total_completion_tokens = sum(msg.completion_tokens or 0 for msg in messages)
    total_all_tokens = sum(msg.total_tokens or 0 for msg in messages)
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
        "message_count": len(messages)
    }
