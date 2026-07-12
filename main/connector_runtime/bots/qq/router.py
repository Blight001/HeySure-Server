import binascii
import json
import threading
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session, select

from api.database import get_session
from api.database import engine
from api.models import AssistantAIConfig, ChatMessage, ChatMessageCreate, ChatRun, ChatSession, User
from api.services.access.access_guards import get_ai_config_or_404
from gateway.routers.auth import get_current_user
from api.chat_runtime.run_state import _RUN_THREADS
from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
from api.core.settings import settings
from ai_runtime.inference.core import _run_worker
from api.services.chat.chat_persistence import _save_message
from ._config import read_qq_config
from .long_connection import get_qq_long_connection_state
from .routes_store import register_qq_session_route
from .service import diagnose_qq_config, parse_qq_text_event, send_qq_text_message
from connector_runtime.bots.session_cursor import get_active_session_id
import logging


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/qq"
QQ_BUSY_REPLY = "稍等，AI正在调用工具中。"
_QQ_DEFERRED_LOCK = threading.Lock()
_QQ_DEFERRED_SESSIONS: set[str] = set()
_LAST_CALLBACKS: Dict[int, Dict[str, Any]] = {}


def _ed25519_private_key_from_secret(secret: str):
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"cryptography is required for QQ signature: {exc}")
    seed = str(secret or "")
    if not seed:
        raise HTTPException(status_code=400, detail="QQ App Secret is required")
    while len(seed.encode("utf-8")) < 32:
        seed += seed
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed.encode("utf-8")[:32])


def _qq_validation_signature(secret: str, event_ts: str, plain_token: str) -> str:
    private_key = _ed25519_private_key_from_secret(secret)
    return private_key.sign(f"{event_ts}{plain_token}".encode("utf-8")).hex()


def _verify_qq_signature(cfg: AssistantAIConfig, request: Request, body: bytes) -> None:
    signature = str(request.headers.get("X-Signature-Ed25519") or "").strip()
    timestamp = str(request.headers.get("X-Signature-Timestamp") or "").strip()
    if not signature or not timestamp:
        raise HTTPException(status_code=403, detail="Missing QQ callback signature")
    try:
        sig = binascii.unhexlify(signature)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid QQ callback signature encoding")
    if len(sig) != 64:
        raise HTTPException(status_code=403, detail="Invalid QQ callback signature length")
    public_key = _ed25519_private_key_from_secret(
        str(read_qq_config(cfg).get("app_secret") or "")
    ).public_key()
    try:
        public_key.verify(sig, timestamp.encode("utf-8") + body)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid QQ callback signature")


def _build_qq_runtime_prompt(base_prompt: str, event: Dict[str, str]) -> str:
    target = event.get("target_id") or ""
    target_type = event.get("target_type") or ""
    return (
        f"{base_prompt}\n\n"
        "[QQ机器人通知前置模板]\n"
        "本轮消息来自 QQ 机器人事件回调。请直接生成要回复给 QQ 用户或群聊的内容，保持清晰、可直接发送。\n"
        "服务端只会把实际回复内容发回来源会话，不需要输出处理状态或工具调用状态。\n"
        "除非用户明确要求额外通知其他机器人会话，否则不要调用 MCP 工具 `message.send+to+user`，避免重复回复。\n"
        "如果用户要求忘掉/清除/重置/忽略此前对话或上下文，请先调用 MCP 工具 "
        "`conversation.manage`，使用 `action=clear`；默认只删除当前用户消息之前的内容。\n"
        "当用户想要 列出/读取/切换/新开 对话或会话时，调用 MCP 工具 "
        "`conversation.manage`（action=list 列出该 AI 的全部对话、detail 读取指定对话详情、switch 切到指定对话、"
        "new 新建并切换）；切换在用户的下一条消息生效，本条回复仍发回当前对话。\n"
        f"- 来源接收目标: {target_type}:{target}\n"
        "- 默认回传策略: 优先使用收到事件里的 msg_id 做被动回复。"
    )


def _send_qq_text(
    *,
    user_id: int,
    ai_config_id: int,
    target_id: str,
    target_type: str,
    text: str,
    msg_id: str = "",
    event_id: str = "",
    msg_seq: Optional[int] = None,
) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    try:
        send_qq_text_message(
            user_id,
            ai_config_id,
            text=body,
            target_id=target_id,
            target_type=target_type,
            msg_id=msg_id,
            event_id=event_id,
            msg_seq=msg_seq,
        )
        return True
    except Exception as exc:
        logger.exception(f"send failed config_id={ai_config_id}: {exc}")
        return False


def _start_qq_worker(worker_kwargs: Dict[str, Any]) -> str:
    import json as _json
    from ai_runtime.worker import notify_queue

    run_id = str(worker_kwargs["run_id"])
    if settings.ai_dispatch_mode == "remote":
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

    worker = threading.Thread(target=_run_worker, kwargs=worker_kwargs, daemon=True)
    _RUN_THREADS[run_id] = worker
    worker.start()
    return run_id


def _qq_session_has_live_run(
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
        if str(row.status or "") == "queued" and now - float(row.created_at or now) < 5:
            return True
    return False


def _wait_for_qq_idle_then_run(*, deferred_key: str, worker_kwargs: Dict[str, Any]) -> None:
    try:
        send_kwargs = {
            "user_id": int(worker_kwargs["user_id"]),
            "ai_config_id": int(worker_kwargs["ai_config_id"]),
            "ai_kind": str(worker_kwargs["ai_kind"]),
            "session_id": str(worker_kwargs["session_id"]),
        }
        deadline = time.time() + 24 * 60 * 60
        while time.time() < deadline:
            if not _qq_session_has_live_run(**send_kwargs):
                break
            time.sleep(0.5)
        if _qq_session_has_live_run(**send_kwargs):
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
        _start_qq_worker(worker_kwargs)
    finally:
        with _QQ_DEFERRED_LOCK:
            _QQ_DEFERRED_SESSIONS.discard(deferred_key)


@router.post("/events/{config_id}")
async def receive_qq_event(config_id: int, request: Request):
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid QQ event payload")
    return handle_qq_event_payload(config_id, payload, request=request, raw_body=body)


@router.get("/diagnose/{config_id}")
async def diagnose_qq_bot(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    out = diagnose_qq_config(user.id, config_id)
    out["bot_status"] = get_qq_long_connection_state(config_id)
    out["status"] = out["bot_status"].get("status") or out.get("status") or "failed"
    out["callback_path"] = f"/api/qq/events/{config_id}"
    out["last_callback"] = _LAST_CALLBACKS.get(config_id)
    out["connection_mode"] = "botpy_websocket"
    out["note"] = (
        "QQ uses a botpy WebSocket long connection for inbound events. "
        "The webhook path is kept only for compatibility and diagnostics."
    )
    return out


@router.post("/diagnose/{config_id}/send-test")
async def diagnose_qq_send_test(
    config_id: int,
    body: Dict[str, Any],
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    result = send_qq_text_message(
        user.id,
        config_id,
        text=str(body.get("text") or "HeySure QQ bot send test").strip(),
        target_id=str(body.get("target_id") or "").strip(),
        target_type=str(body.get("target_type") or "").strip(),
    )
    return {"success": True, "send_result": result}


def handle_qq_event_payload(
    config_id: int,
    payload: Dict[str, Any],
    *,
    request: Optional[Request] = None,
    raw_body: Optional[bytes] = None,
) -> Dict[str, Any]:
    with Session(engine) as session:
        cfg = get_ai_config_or_404(session, config_id)
        if str(cfg.bot_channel or "feishu").strip().lower() != "qq":
            raise HTTPException(status_code=400, detail="QQ bot is not the active channel for this AI")
        if not read_qq_config(cfg).get("enabled"):
            raise HTTPException(status_code=400, detail="QQ bot is disabled for this AI")
        op = int(payload.get("op") or 0)
        event_type = str(payload.get("t") or "").strip()
        _LAST_CALLBACKS[config_id] = {
            "received_at": time.time(),
            "op": op,
            "event_type": event_type,
            "has_signature": bool(request.headers.get("X-Signature-Ed25519")) if request is not None else False,
            "body_bytes": len(raw_body or b""),
        }
        logger.debug(f"config_id={config_id} op={op} event_type={event_type or '-'} bytes={len(raw_body or b'')}")
        if request is not None and raw_body is not None and op != 13:
            _verify_qq_signature(cfg, request, raw_body)

        if op == 13:
            data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
            plain_token = str(data.get("plain_token") or "").strip()
            event_ts = str(data.get("event_ts") or "").strip()
            if not plain_token or not event_ts:
                raise HTTPException(status_code=400, detail="Invalid QQ validation payload")
            logger.debug(f"validation ok config_id={config_id}")
            return {
                "plain_token": plain_token,
                "signature": _qq_validation_signature(
                    str(read_qq_config(cfg).get("app_secret") or ""), event_ts, plain_token
                ),
            }

        event = parse_qq_text_event(payload)
        if not event:
            logger.debug(f"ignored config_id={config_id} op={op} event_type={event_type or '-'}")
            return {"op": 12, "d": 0}

        target_id = event.get("target_id") or ""
        target_type = event.get("target_type") or "c2c"
        qq_message_id = event.get("message_id") or ""
        qq_event_id = event.get("event_id") or ""
        ai_kind = "assistant" if cfg.ai_role == "assistant_admin" else "core"
        session_key = f"{target_type}_{target_id}"
        home_session_id = f"qq_{config_id}_{session_key}"
        # Resolve which session in the shared pool this user's message lands in.
        # Defaults to the user's home session; follows the cursor when the AI
        # has switched/created another conversation for this identity.
        session_id = get_active_session_id(
            session,
            channel="qq",
            user_id=int(cfg.user_id),
            ai_config_id=int(cfg.id or config_id),
            ai_kind=ai_kind,
            identity_key=target_id,
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
            else f"QQ对话 {session_key}"
        )
        register_qq_session_route(
            session,
            user_id=int(cfg.user_id),
            ai_config_id=int(cfg.id or config_id),
            ai_kind=ai_kind,
            session_id=session_id,
            target_id=target_id,
            target_type=target_type,
            source_message_id=qq_message_id,
            source_event_id=qq_event_id,
            next_msg_seq=1,
        )

        user = session.get(User, cfg.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        _, _, _, _, system_prompt = _resolve_ai_runtime(session, user, ai_kind, cfg.id)
        merged_system_prompt = _build_qq_runtime_prompt(system_prompt, event)
        inbound_tag = f"qq_inbound:{qq_message_id or qq_event_id}" if (qq_message_id or qq_event_id) else "qq_inbound"

        if qq_message_id or qq_event_id:
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
                return {"op": 12, "d": 0, "duplicate": True, "run_id": active.run_id if active else None}

        inbound_msg = _save_message(
            session,
            cfg.user_id,
            ChatMessageCreate(
                role="user",
                content=event["text"],
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
        cfg_id = int(cfg.id or 0)
        cfg_user_id = int(cfg.user_id)
        inbound_message_id = int(inbound_msg.id or 0)
        if active:
            _send_qq_text(
                user_id=cfg_user_id,
                ai_config_id=cfg_id,
                target_id=target_id,
                target_type=target_type,
                text=QQ_BUSY_REPLY,
                msg_id=qq_message_id,
                event_id=qq_event_id,
                msg_seq=1 if qq_message_id else None,
            )
            register_qq_session_route(
                session,
                user_id=cfg_user_id,
                ai_config_id=cfg_id,
                ai_kind=ai_kind,
                session_id=session_id,
                target_id=target_id,
                target_type=target_type,
                source_message_id=qq_message_id,
                source_event_id=qq_event_id,
                next_msg_seq=2,
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
            with _QQ_DEFERRED_LOCK:
                should_start_deferred = deferred_key not in _QQ_DEFERRED_SESSIONS
                if should_start_deferred:
                    _QQ_DEFERRED_SESSIONS.add(deferred_key)
            if should_start_deferred:
                threading.Thread(
                    target=_wait_for_qq_idle_then_run,
                    kwargs={"deferred_key": deferred_key, "worker_kwargs": worker_kwargs},
                    daemon=True,
                ).start()
            return {"op": 12, "d": 0, "run_id": active.run_id, "already_active": True, "queued_after_active": True}

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

    worker_kwargs = {
        "run_id": run_id,
        "user_id": cfg_user_id,
        "ai_config_id": cfg_id,
        "ai_kind": ai_kind,
        "session_id": session_id,
        "session_name": session_name,
        "model_user_content": event["text"],
        "merged_system_prompt": merged_system_prompt,
        "max_steps": None,
        "current_user_message_id": inbound_message_id,
    }
    _start_qq_worker(worker_kwargs)
    return {"op": 12, "d": 0, "run_id": run_id}
