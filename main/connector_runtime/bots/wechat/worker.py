"""Launch a durable AI run for a normalized WeChat inbound message."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from ai_runtime.inference.core import _run_worker
from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
from api.chat_runtime.run_state import _RUN_THREADS
from api.core.settings import settings
from api.database import engine
from api.models import AssistantAIConfig, ChatRun, User


def active_run(session: Session, *, user_id: int, config_id: int, ai_kind: str, session_id: str) -> Optional[ChatRun]:
    return session.exec(select(ChatRun).where(
        ChatRun.user_id == user_id,
        ChatRun.ai_config_id == config_id,
        ChatRun.ai_kind == ai_kind,
        ChatRun.session_id == session_id,
        ChatRun.status.in_(["queued", "running"]),
    )).first()


def _start_worker(kwargs: Dict[str, Any]) -> str:
    from ai_runtime.worker import notify_queue

    run_id = str(kwargs["run_id"])
    if settings.ai_dispatch_mode == "remote":
        extras = {key: kwargs.get(key) for key in (
            "model_user_content", "merged_system_prompt", "max_steps", "current_user_message_id"
        ) if kwargs.get(key) is not None}
        with Session(engine) as session:
            row = session.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
            if row:
                row.worker_kwargs_json = json.dumps(extras, ensure_ascii=False)
                session.add(row)
                session.commit()
        notify_queue(run_id)
        return run_id
    worker = threading.Thread(target=_run_worker, kwargs=kwargs, daemon=True)
    _RUN_THREADS[run_id] = worker
    worker.start()
    return run_id


def _wait_until_idle(*, user_id: int, config_id: int, ai_kind: str, session_id: str) -> bool:
    deadline = time.time() + 600
    while time.time() < deadline:
        with Session(engine) as session:
            if not active_run(session, user_id=user_id, config_id=config_id, ai_kind=ai_kind, session_id=session_id):
                return True
        time.sleep(1)
    return False


def _runtime_prompt(base: str) -> str:
    return (
        f"{base}\n\n[微信机器人前置模板]\n"
        "本轮消息来自腾讯 iLink 微信机器人私聊。请直接生成可发送给微信用户的回复。\n"
        "不要输出工具调用状态，也不要重复调用消息发送工具向当前用户发送相同回复。\n"
        "当前微信通道仅支持私聊。"
    )


def launch_message_run(
    *,
    user_id: int,
    config_id: int,
    ai_kind: str,
    session_id: str,
    session_name: str,
    message_id: int,
    model_content: Any,
    wait_for_idle: bool,
) -> Optional[str]:
    if wait_for_idle and not _wait_until_idle(
        user_id=user_id, config_id=config_id, ai_kind=ai_kind, session_id=session_id
    ):
        return None
    with Session(engine) as session:
        cfg = session.get(AssistantAIConfig, config_id)
        user = session.get(User, user_id)
        if not cfg or not user:
            return None
        if active_run(session, user_id=user_id, config_id=config_id, ai_kind=ai_kind, session_id=session_id):
            return None
        _, _, _, _, system_prompt = _resolve_ai_runtime(session, user, ai_kind, config_id, session_id)
        run_id = f"run_{uuid.uuid4().hex}"
        session.add(ChatRun(
            run_id=run_id,
            user_id=user_id,
            ai_config_id=config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            status="queued",
            stop_requested=False,
        ))
        session.commit()
    return _start_worker({
        "run_id": run_id,
        "user_id": user_id,
        "ai_config_id": config_id,
        "ai_kind": ai_kind,
        "session_id": session_id,
        "session_name": session_name,
        "model_user_content": model_content,
        "merged_system_prompt": _runtime_prompt(system_prompt),
        "max_steps": None,
        "current_user_message_id": message_id,
    })
