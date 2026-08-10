"""Create chat runs and persist their initial workspace attachments."""

IS_ROUTER_ENTRY = False

import json
import threading
import uuid

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from ai_runtime.inference.core import _run_worker
from ai_runtime.worker import notify_queue
from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
from api.core.settings import settings
from api.database import get_session
from api.models import ChatMessageCreate, ChatRun
from api.services.chat.chat_attachments import (
    bind_message_attachments,
    model_attachment_section,
    resolve_attachment_refs,
)
from api.services.chat.chat_persistence import _save_message
from .auth import get_current_user
from .chat_base import _RUN_THREADS, router


def _selected_tools(raw):
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="selected_mcp_tools must be a list")
    return list(dict.fromkeys(
        str(name).strip() for name in raw[:500] if str(name).strip()
    ))


def _reject_active_run(session, user_id, context):
    statement = select(ChatRun).where(
        ChatRun.user_id == user_id,
        ChatRun.ai_kind == context["ai_kind"],
        ChatRun.session_id == context["session_id"],
        ChatRun.status.in_(["queued", "running"]),
    )
    ai_config_id = context["ai_config_id"]
    statement = statement.where(
        ChatRun.ai_config_id == ai_config_id
        if ai_config_id is not None
        else ChatRun.ai_config_id.is_(None)
    )
    if session.exec(statement).first():
        raise HTTPException(status_code=409, detail="A run is already active in this session")


def _prepare_context(session, user, req):
    context = {
        "ai_config_id": req.get("ai_config_id"),
        "ai_kind": req.get("ai_kind", "assistant"),
        "session_id": str(req.get("session_id") or "default"),
        "session_name": str(req.get("session_name") or "未命名会话"),
        "visible_content": str(req.get("visible_content") or "").strip(),
        "model_content": str(req.get("model_content") or req.get("visible_content") or "").strip(),
        "selected_mcp_tools": _selected_tools(req.get("selected_mcp_tools")),
    }
    records = resolve_attachment_refs(
        user_id=user.id,
        ai_config_id=context["ai_config_id"],
        raw=req.get("attachments"),
    )
    section = model_attachment_section(records)
    if section:
        context["model_content"] = "\n\n".join(
            part for part in (context["model_content"], section) if part
        )
    if not context["model_content"]:
        raise HTTPException(status_code=400, detail="Message content is required")
    if not context["visible_content"]:
        context["visible_content"] = f"已上传 {len(records)} 个附件"
    _, _, _, _, system_prompt = _resolve_ai_runtime(
        session,
        user,
        context["ai_kind"],
        context["ai_config_id"],
        context["session_id"],
    )
    incoming = req.get("system_messages") or []
    trimmed = [str(value).strip() for value in incoming if str(value).strip()] if isinstance(incoming, list) else []
    context["merged_system_prompt"] = (
        f"{system_prompt}\n\n" + "\n\n".join(trimmed)
        if trimmed else system_prompt
    )
    context["attachment_records"] = records
    return context


def _persist_run(session, user, req, context):
    message = _save_message(
        session,
        user.id,
        ChatMessageCreate(
            role="user",
            content=context["visible_content"],
            tags=str(req.get("visible_tags") or "").strip(),
            ai_config_id=context["ai_config_id"],
            ai_kind=context["ai_kind"],
            session_id=context["session_id"],
            session_name=context["session_name"],
        ),
    )
    bind_message_attachments(
        session,
        message_id=int(message.id),
        user_id=user.id,
        ai_config_id=context["ai_config_id"],
        records=context["attachment_records"],
    )
    run_id = f"run_{uuid.uuid4().hex}"
    extras = {
        "model_user_content": context["model_content"],
        "merged_system_prompt": context["merged_system_prompt"],
        "max_steps": req.get("max_steps"),
        "current_user_message_id": message.id,
        "selected_mcp_tools": context["selected_mcp_tools"],
    }
    session.add(ChatRun(
        run_id=run_id,
        user_id=user.id,
        ai_config_id=context["ai_config_id"],
        ai_kind=context["ai_kind"],
        session_id=context["session_id"],
        session_name=context["session_name"],
        status="queued",
        stop_requested=False,
        worker_kwargs_json=json.dumps(extras, ensure_ascii=False),
    ))
    session.commit()
    return run_id, message, extras


def _start_local_worker(run_id, user, context, extras):
    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": user.id,
            "ai_config_id": context["ai_config_id"],
            "ai_kind": context["ai_kind"],
            "session_id": context["session_id"],
            "session_name": context["session_name"],
            **extras,
            "selected_mcp_tools": set(extras["selected_mcp_tools"])
                if extras["selected_mcp_tools"] is not None else None,
        },
        daemon=True,
    )
    worker.start()
    _RUN_THREADS[run_id] = worker


@router.post("/run/start")
def start_chat_run(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    context = _prepare_context(session, user, req)
    _reject_active_run(session, user.id, context)
    run_id, message, extras = _persist_run(session, user, req, context)
    if settings.ai_dispatch_mode == "remote":
        notify_queue(run_id)
    else:
        _start_local_worker(run_id, user, context, extras)
    return {"run_id": run_id, "status": "queued", "user_message_id": message.id}
