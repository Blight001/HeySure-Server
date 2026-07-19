import threading
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig, ChatMessage, ChatMessageCreate, ChatRun, ChatSession, User
from api.services.access.access_guards import get_ai_config_or_404
from api.core.settings import settings
from api.services.chat.chat_persistence import _save_message
from api.chat_runtime.run_state import _RUN_THREADS
from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
from ai_runtime.inference.core import _run_worker
from ._config import read_feishu_config
from .routes_store import register_feishu_session_route
from .service import parse_feishu_text_event, send_feishu_text_message
from connector_runtime.bots.session_cursor import get_active_session_id
from connector_runtime.bots.commands import handle_bot_command
import logging


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/feishu"
# Feishu text messages have a length cap; split only inside one logical reply segment.
FEISHU_TEXT_MAX_CHARS = 1800
FEISHU_BUSY_REPLY = "稍等，AI正在调用工具中。"
_FEISHU_DEFERRED_LOCK = threading.Lock()
_FEISHU_DEFERRED_SESSIONS: set[str] = set()


def _verify_token(cfg: AssistantAIConfig, payload: Dict[str, Any]) -> None:
    expected = str(read_feishu_config(cfg).get("verification_token") or "").strip()
    if not expected:
        return
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    token = str(payload.get("token") or header.get("token") or "").strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")


def _build_feishu_runtime_prompt(base_prompt: str, event: Dict[str, str]) -> str:
    chat_id = event.get("chat_id") or ""
    open_id = event.get("open_id") or ""
    target_hint = f"chat_id={chat_id}" if chat_id else f"open_id={open_id}"
    return (
        f"{base_prompt}\n\n"
        "[飞书通知前置模板]\n"
        "本轮消息来自飞书事件回调。请直接生成要回复给飞书用户的内容，保持清晰、可直接发送。\n"
        "服务端只会把实际回复内容发回来源会话，不需要输出处理状态或工具调用状态。\n"
        "除非用户明确要求额外通知其他飞书会话，否则不要调用 MCP 工具 `message.send+to` 给用户发消息，避免重复回复。\n"
        "如果用户要求忘掉/清除/重置/忽略此前对话或上下文，请先调用 MCP 工具 "
        "`conversation.manage`，使用 `action=clear`；默认只删除当前用户消息之前的内容。\n"
        "当用户想要 列出/读取/切换/新开 对话或会话时，调用 MCP 工具 "
        "`conversation.manage`（action=list 列出该 AI 的全部对话、detail 读取指定对话详情、switch 切到指定对话、"
        "new 新建并切换）；切换在用户的下一条消息生效，本条回复仍发回当前对话。\n"
        f"- 来源接收目标: {target_hint or '未识别'}\n"
        "- 默认回传策略: 优先使用 chat_id；chat_id 为空时使用 open_id 且 receive_id_type=open_id。"
    )


def _send_feishu_text(
    *,
    user_id: int,
    ai_config_id: int,
    receive_id: str,
    receive_id_type: str,
    text: str,
) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    ok = False
    for start in range(0, len(body), FEISHU_TEXT_MAX_CHARS):
        chunk = body[start:start + FEISHU_TEXT_MAX_CHARS].strip()
        if not chunk:
            continue
        try:
            send_feishu_text_message(
                user_id,
                ai_config_id,
                text=chunk,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
            )
            ok = True
        except Exception as exc:
            logger.exception(f"send failed config_id={ai_config_id}: {exc}")
            return ok
    return ok


def _start_feishu_worker(worker_kwargs: Dict[str, Any]) -> str:
    import json as _json
    from ai_runtime.worker import notify_queue

    run_id = str(worker_kwargs["run_id"])
    if settings.ai_dispatch_mode == "remote":
        # Persist non-default kwargs so ai-runtime can rebuild the call.
        # Feishu specifically computes a custom merged_system_prompt from the
        # inbound event — losing that would change AI behavior.
        extras = {
            k: worker_kwargs.get(k)
            for k in (
                "model_user_content",
                "merged_system_prompt",
                "max_steps",
                "current_user_message_id",
            )
            if worker_kwargs.get(k) is not None
        }
        if extras:
            try:
                with Session(engine) as bg:
                    row = bg.exec(select(ChatRun).where(ChatRun.run_id == run_id)).first()
                    if row:
                        row.worker_kwargs_json = _json.dumps(extras, ensure_ascii=False)
                        bg.add(row)
                        bg.commit()
            except Exception as exc:
                logger.exception(f"persist worker_kwargs failed for {run_id}: {exc}")
        notify_queue(run_id)
        return run_id

    worker = threading.Thread(
        target=_run_worker,
        kwargs=worker_kwargs,
        daemon=True,
    )
    _RUN_THREADS[run_id] = worker
    worker.start()
    return run_id


def _feishu_session_has_live_run(
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
) -> bool:
    now = time.time()
    with Session(engine) as session:
        rows = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == user_id,
                ChatRun.ai_config_id == ai_config_id,
                ChatRun.ai_kind == ai_kind,
                ChatRun.session_id == session_id,
                ChatRun.status.in_(["queued", "running"]),
            ).order_by(ChatRun.updated_at.desc())
        ).all()
    for row in rows:
        run_id = str(row.run_id or "")
        worker = _RUN_THREADS.get(run_id)
        if worker and worker.is_alive():
            return True
        # A newly inserted run may not have reached _RUN_THREADS yet.
        if str(row.status or "") == "queued" and now - float(row.created_at or now) < 5:
            return True
    return False


def _wait_for_feishu_idle_then_run(
    *,
    deferred_key: str,
    worker_kwargs: Dict[str, Any],
) -> None:
    try:
        send_kwargs = {
            "user_id": int(worker_kwargs["user_id"]),
            "ai_config_id": int(worker_kwargs["ai_config_id"]),
            "ai_kind": str(worker_kwargs["ai_kind"]),
            "session_id": str(worker_kwargs["session_id"]),
        }
        deadline = time.time() + 24 * 60 * 60
        while time.time() < deadline:
            if not _feishu_session_has_live_run(**send_kwargs):
                break
            time.sleep(0.5)
        if _feishu_session_has_live_run(**send_kwargs):
            logger.debug(f"deferred run timeout session={send_kwargs['session_id']}")
            return

        run_id = f"run_{uuid.uuid4().hex}"
        with Session(engine) as session:
            row = ChatRun(
                run_id=run_id,
                user_id=send_kwargs["user_id"],
                ai_config_id=send_kwargs["ai_config_id"],
                ai_kind=send_kwargs["ai_kind"],
                session_id=send_kwargs["session_id"],
                session_name=str(worker_kwargs.get("session_name") or ""),
                status="queued",
                stop_requested=False,
            )
            session.add(row)
            session.commit()
        worker_kwargs["run_id"] = run_id
        _start_feishu_worker(worker_kwargs)
    finally:
        with _FEISHU_DEFERRED_LOCK:
            _FEISHU_DEFERRED_SESSIONS.discard(deferred_key)


@router.post("/events/{config_id}")
async def receive_feishu_event(config_id: int, request: Request):
    payload = await request.json()
    result = handle_feishu_event_payload(config_id, payload, verify_token=True)
    return result


def handle_feishu_event_payload(config_id: int, payload: Dict[str, Any], verify_token: bool = True) -> Dict[str, Any]:
    with Session(engine) as session:
        cfg = get_ai_config_or_404(session, config_id)
        if str(cfg.bot_channel or "feishu").strip().lower() != "feishu":
            raise HTTPException(status_code=400, detail="Feishu bot is not the active channel for this AI")
        if not read_feishu_config(cfg).get("enabled"):
            raise HTTPException(status_code=400, detail="Feishu bot is disabled for this AI")
        if verify_token:
            _verify_token(cfg, payload)

        challenge = payload.get("challenge")
        if challenge:
            return {"challenge": challenge}

        event = parse_feishu_text_event(payload)
        if not event:
            return {"success": True, "ignored": True}

        chat_id = event.get("chat_id") or ""
        open_id = event.get("open_id") or ""
        feishu_message_id = event.get("message_id") or ""
        ai_kind = "assistant" if cfg.ai_role == "assistant_admin" else "core"
        session_key = chat_id or open_id or "unknown"
        home_session_id = f"feishu_{config_id}_{session_key}"
        receive_id = chat_id or open_id
        receive_id_type = "chat_id" if chat_id else "open_id"
        # Resolve which session in the shared pool this user's message lands in
        # (cursor-driven; defaults to the user's home session).
        session_id = get_active_session_id(
            session,
            channel="feishu",
            user_id=int(cfg.user_id),
            ai_config_id=int(cfg.id or config_id),
            ai_kind=ai_kind,
            identity_key=receive_id,
            default=home_session_id,
        )
        existing_session = session.exec(
            select(ChatSession).where(
                ChatSession.user_id == int(cfg.user_id),
                ChatSession.ai_config_id == int(cfg.id or config_id),
                ChatSession.ai_kind == ai_kind,
                ChatSession.session_id == session_id,
            )
        ).first()
        session_name = (
            str(existing_session.session_name) if existing_session and existing_session.session_name
            else f"飞书对话 {session_key}"
        )
        register_feishu_session_route(
            session,
            user_id=int(cfg.user_id),
            ai_config_id=int(cfg.id or config_id),
            ai_kind=ai_kind,
            session_id=session_id,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )
        visible_content = event["text"]
        model_content = visible_content

        user = session.get(User, cfg.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        command_result = handle_bot_command(
            session,
            text=event["text"],
            channel="feishu",
            user=user,
            cfg=cfg,
            ai_kind=ai_kind,
            identity_key=receive_id,
            current_session_id=session_id,
            current_session_name=session_name,
            home_session_id=home_session_id,
        )
        if command_result is not None:
            _send_feishu_text(
                user_id=int(cfg.user_id),
                ai_config_id=int(cfg.id or config_id),
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                text=command_result.text,
            )
            return {
                "success": True,
                "command": command_result.command,
                "command_handled": True,
            }
        _, _, _, _, system_prompt = _resolve_ai_runtime(
            session, user, ai_kind, cfg.id, session_id
        )
        merged_system_prompt = _build_feishu_runtime_prompt(system_prompt, event)
        inbound_tag = f"feishu_inbound:{feishu_message_id}" if feishu_message_id else "feishu_inbound"

        if feishu_message_id:
            existing_inbound = session.exec(
                select(ChatMessage).where(
                    ChatMessage.user_id == cfg.user_id,
                    ChatMessage.ai_config_id == cfg.id,
                    ChatMessage.ai_kind == ai_kind,
                    ChatMessage.session_id == session_id,
                    ChatMessage.tags == inbound_tag,
                )
            ).first()
            if existing_inbound:
                active = session.exec(
                    select(ChatRun).where(
                        ChatRun.user_id == cfg.user_id,
                        ChatRun.ai_config_id == cfg.id,
                        ChatRun.ai_kind == ai_kind,
                        ChatRun.session_id == session_id,
                        ChatRun.status.in_(["queued", "running"]),
                    ).order_by(ChatRun.updated_at.desc())
                ).first()
                return {
                    "success": True,
                    "duplicate": True,
                    "message_id": feishu_message_id,
                    "run_id": active.run_id if active else None,
                }

        inbound_msg = _save_message(
            session,
            cfg.user_id,
            ChatMessageCreate(
                role="user",
                content=visible_content,
                ai_config_id=cfg.id,
                ai_kind=ai_kind,
                session_id=session_id,
                session_name=session_name,
                tags=inbound_tag,
                total_tokens=0,
            ),
        )

        active = session.exec(
            select(ChatRun).where(
                ChatRun.user_id == cfg.user_id,
                ChatRun.ai_config_id == cfg.id,
                ChatRun.ai_kind == ai_kind,
                ChatRun.session_id == session_id,
                ChatRun.status.in_(["queued", "running"]),
            )
        ).first()
        if active:
            cfg_id = int(cfg.id or 0)
            cfg_user_id = int(cfg.user_id)
            inbound_message_id = int(inbound_msg.id or 0)
            _send_feishu_text(
                user_id=cfg_user_id,
                ai_config_id=cfg_id,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                text=FEISHU_BUSY_REPLY,
            )
            worker_kwargs = {
                "run_id": "",
                "user_id": cfg_user_id,
                "ai_config_id": cfg_id,
                "ai_kind": ai_kind,
                "session_id": session_id,
                "session_name": session_name,
                "model_user_content": None,
                "merged_system_prompt": merged_system_prompt,
                "max_steps": None,
                "current_user_message_id": inbound_message_id,
            }
            deferred_key = f"{cfg_user_id}:{cfg_id}:{ai_kind}:{session_id}"
            with _FEISHU_DEFERRED_LOCK:
                should_start_deferred = deferred_key not in _FEISHU_DEFERRED_SESSIONS
                if should_start_deferred:
                    _FEISHU_DEFERRED_SESSIONS.add(deferred_key)
            if should_start_deferred:
                threading.Thread(
                    target=_wait_for_feishu_idle_then_run,
                    kwargs={
                        "deferred_key": deferred_key,
                        "worker_kwargs": worker_kwargs,
                    },
                    daemon=True,
                ).start()
            return {"success": True, "run_id": active.run_id, "already_active": True, "queued_after_active": True}

        run_id = f"run_{uuid.uuid4().hex}"
        row = ChatRun(
            run_id=run_id,
            user_id=cfg.user_id,
            ai_config_id=cfg.id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
            status="queued",
            stop_requested=False,
        )
        session.add(row)
        session.commit()
        cfg_id = int(cfg.id or 0)
        cfg_user_id = int(cfg.user_id)
        inbound_message_id = int(inbound_msg.id or 0)

    worker_kwargs = {
        "run_id": run_id,
        "user_id": cfg_user_id,
        "ai_config_id": cfg_id,
        "ai_kind": ai_kind,
        "session_id": session_id,
        "session_name": session_name,
        "model_user_content": model_content,
        "merged_system_prompt": merged_system_prompt,
        "max_steps": None,
        "current_user_message_id": inbound_message_id,
    }
    _start_feishu_worker(worker_kwargs)
    return {"success": True, "run_id": run_id}
