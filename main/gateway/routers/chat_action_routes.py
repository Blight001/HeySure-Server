"""``/api/chat`` action routes: start/stop/inspect chat runs (``/run/*``), save,
delete, recall, tag and clear messages, serve message media and workspace files,
and stream run output (SSE) by dispatching to the AI runtime worker."""

IS_ROUTER_ENTRY = False

import json
import mimetypes
import os
import threading
import time
import uuid
from typing import List, Optional

from fastapi import Depends, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse  # 用于 /stream 端点的 SSE 流式响应
from sqlmodel import Session, select

from api.database import get_session
from api.runtime.http_client import ai_http_post
from mcp_runtime.mcp import get_project_root, registry
from api.models import AssistantAIConfig, ChatMessage, ChatMessageCreate, ChatMessageUpdate, ChatRun
from .auth import get_current_user
from ai_runtime.worker import notify_queue
from api.core.settings import settings
from api.services.model_presets import is_cli_base_url, resolve_model_preset
from api.services.chat import chat_inject
from api.services.chat.chat_media import delete_message_media, get_message_media
from .chat_base import _RUN_LIVE_STATE, _RUN_STATE_LOCK, _RUN_THREADS, router
from api.services.chat.chat_persistence import _append_usage_snapshot, _rebuild_usage_snapshots, _save_message, _upsert_session
from api.chat_runtime.chat_prompt_utils import (
    _append_prompt_section,
    _build_mcp_stream_warning,
    _clear_run_live_text,
    _strip_runtime_injected_sections,
)
from api.chat_runtime.chat_runtime_helpers import _resolve_ai_runtime
from ai_runtime.inference.core import _raise_for_upstream_error, _run_worker


MAX_WORKSPACE_PREVIEW_BYTES = 1024 * 1024
WORKSPACE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
WORKSPACE_VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v"}
WORKSPACE_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".html", ".htm", ".css", ".scss",
    ".bat", ".cmd", ".ps1", ".sh", ".sql",
}


def _safe_user_workspace_path(user_id: int, rel: str) -> str:
    root = os.path.realpath(get_project_root(user_id, None))
    clean = str(rel or "").strip().replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(root, clean))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="非法的文件路径")
    return full


def _workspace_rel_path(user_id: int, full: str) -> str:
    root = os.path.realpath(get_project_root(user_id, None))
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    return "" if rel == "." else rel


def _workspace_file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in WORKSPACE_IMAGE_EXTS:
        return "image"
    if ext in WORKSPACE_VIDEO_EXTS:
        return "video"
    if ext in WORKSPACE_TEXT_EXTS:
        return "text"
    mime, _ = mimetypes.guess_type(name)
    if mime and (mime.startswith("text/") or mime in ("application/json", "application/xml", "application/javascript")):
        return "text"
    return "text" if not ext else "binary"


def _is_probably_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _ai_dispatch_mode() -> str:
    """Return 'remote' when a dedicated ai-runtime service consumes the queue.

    In 'remote' mode, api-gateway only enqueues queued ChatRun rows + NOTIFY;
    it does NOT spawn a local thread to run the worker. In 'local' mode
    (the historical monolith), api-gateway spawns a worker thread itself.
    """
    return "remote" if settings.ai_dispatch_mode == "remote" else "local"


def _build_run_status_payload(row: ChatRun, live: dict) -> dict:
    live_text = str(live.get("text") or "")
    live_reasoning = str(live.get("reasoning") or "")
    live_delta = ""
    return {
        "run_id": row.run_id,
        "status": row.status,
        "stop_requested": row.stop_requested,
        "error_message": row.error_message,
        "session_id": row.session_id,
        "ai_config_id": row.ai_config_id,
        "ai_kind": row.ai_kind,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "live_text": live_text,
        "live_delta": live_delta,
        "live_len": len(live_text),
        "live_reasoning": live_reasoning,
        "live_reasoning_len": len(live_reasoning),
        "live_updated_at": live.get("updated_at"),
        "live_phase": str(live.get("phase") or "idle"),
        "current_tool": str(live.get("current_tool") or ""),
        "live_prompt_tokens": int(live.get("pending_prompt_tokens") or 0),
        "live_completion_tokens": int(live.get("pending_completion_tokens") or 0),
        "live_total_tokens": int(live.get("pending_total_tokens") or 0),
    }


@router.post("/run/start")
def start_chat_run(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    ai_config_id = req.get("ai_config_id")
    ai_kind = req.get("ai_kind", "assistant")
    session_id = str(req.get("session_id") or "default")
    session_name = str(req.get("session_name") or "未命名会话")
    visible_content = str(req.get("visible_content") or "").strip()
    model_content = str(req.get("model_content") or visible_content).strip()
    if not model_content:
        raise HTTPException(status_code=400, detail="Message content is required")

    active_stmt = select(ChatRun).where(
        ChatRun.user_id == user.id,
        ChatRun.ai_kind == ai_kind,
        ChatRun.session_id == session_id,
        ChatRun.status.in_(["queued", "running"]),
    )
    if ai_config_id is not None:
        active_stmt = active_stmt.where(ChatRun.ai_config_id == ai_config_id)
    else:
        active_stmt = active_stmt.where(ChatRun.ai_config_id.is_(None))
    active = session.exec(active_stmt).first()
    if active:
        raise HTTPException(status_code=409, detail="A run is already active in this session")

    incoming_system_messages = req.get("system_messages") or []
    if not isinstance(incoming_system_messages, list):
        incoming_system_messages = []
    _, _, _, _, system_prompt = _resolve_ai_runtime(session, user, ai_kind, ai_config_id)
    trimmed_system = [str(v).strip() for v in incoming_system_messages if str(v).strip()]
    merged_system_prompt = system_prompt
    if trimmed_system:
        merged_system_prompt = f"{system_prompt}\n\n" + "\n\n".join(trimmed_system)

    visible_tags = str(req.get("visible_tags") or "").strip()
    user_msg = _save_message(
        session,
        user.id,
        ChatMessageCreate(
            role="user",
            content=visible_content or model_content,
            tags=visible_tags,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=session_id,
            session_name=session_name,
        ),
    )
    run_id = f"run_{uuid.uuid4().hex}"
    worker_extras = {
        "model_user_content": model_content,
        "merged_system_prompt": merged_system_prompt,
        "max_steps": req.get("max_steps"),
        "current_user_message_id": user_msg.id,
    }
    row = ChatRun(
        run_id=run_id,
        user_id=user.id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
        session_name=session_name,
        status="queued",
        stop_requested=False,
        worker_kwargs_json=json.dumps(worker_extras, ensure_ascii=False),
    )
    session.add(row)
    session.commit()

    if _ai_dispatch_mode() == "remote":
        # ai-runtime will pick the row up via NOTIFY/poll. Skip local thread.
        notify_queue(run_id)
        return {"run_id": run_id, "status": "queued", "user_message_id": user_msg.id}

    worker = threading.Thread(
        target=_run_worker,
        kwargs={
            "run_id": run_id,
            "user_id": user.id,
            "ai_config_id": ai_config_id,
            "ai_kind": ai_kind,
            "session_id": session_id,
            "session_name": session_name,
            "model_user_content": model_content,
            "merged_system_prompt": merged_system_prompt,
            "max_steps": req.get("max_steps"),
            "current_user_message_id": user_msg.id,
        },
        daemon=True,
    )
    worker.start()
    _RUN_THREADS[run_id] = worker
    return {"run_id": run_id, "status": "queued", "user_message_id": user_msg.id}

@router.post("/run/inject")
def inject_chat_message(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    """Deliver a user message into an already-running chat run.

    Instead of blocking until the whole run finishes, the message is persisted
    as a pending user-inject and the running worker picks it up at its next step
    boundary (after a deep-thinking turn or an MCP call). Returns
    ``{"active": false}`` when there is no run to inject into, so the caller can
    fall back to a normal send.
    """
    user = get_current_user(authorization, session)
    ai_config_id = req.get("ai_config_id")
    ai_kind = req.get("ai_kind", "assistant")
    session_id = str(req.get("session_id") or "default")
    session_name = str(req.get("session_name") or "未命名会话")
    content = str(req.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message content is required")

    active = chat_inject.find_live_active_run(
        session,
        user_id=user.id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
    )
    if not active:
        # Nothing in flight — let the caller send this as a normal new run.
        return {"active": False}

    user_msg = chat_inject.queue_pending_inject(
        session,
        user_id=user.id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
        session_name=session_name,
        content=content,
    )
    return {"active": True, "run_id": active.run_id, "user_message_id": user_msg.id}


@router.get("/run/status/{run_id}")
async def get_chat_run(
    run_id: str,
    after: Optional[int] = None,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = session.exec(
        select(ChatRun).where(ChatRun.run_id == run_id, ChatRun.user_id == user.id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    with _RUN_STATE_LOCK:
        live = _RUN_LIVE_STATE.get(run_id) or {}
    payload = _build_run_status_payload(row, live)
    if after is not None and after >= 0 and after <= len(payload["live_text"]):
        payload["live_delta"] = payload["live_text"][after:]
    return payload



@router.get("/run/active")
async def get_active_chat_run(
    session_id: str,
    ai_config_id: Optional[int] = None,
    ai_kind: str = "assistant",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    stmt = select(ChatRun).where(
        ChatRun.user_id == user.id,
        ChatRun.session_id == session_id,
        ChatRun.ai_kind == ai_kind,
        ChatRun.status.in_(["queued", "running"]),
    )
    if ai_config_id is not None:
        stmt = stmt.where(ChatRun.ai_config_id == ai_config_id)
    else:
        stmt = stmt.where(ChatRun.ai_config_id.is_(None))
    row = session.exec(stmt.order_by(ChatRun.updated_at.desc())).first()
    if not row:
        return {"run": None}
    with _RUN_STATE_LOCK:
        live = _RUN_LIVE_STATE.get(row.run_id) or {}
    return {
        "run": {
            "run_id": row.run_id,
            "status": row.status,
            "stop_requested": row.stop_requested,
            "error_message": row.error_message,
            "session_id": row.session_id,
            "ai_config_id": row.ai_config_id,
            "ai_kind": row.ai_kind,
            "updated_at": row.updated_at,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "live_text": str(live.get("text") or ""),
            "live_len": len(str(live.get("text") or "")),
            "live_reasoning": str(live.get("reasoning") or ""),
            "live_reasoning_len": len(str(live.get("reasoning") or "")),
            "live_updated_at": live.get("updated_at"),
            "live_phase": str(live.get("phase") or "idle"),
            "current_tool": str(live.get("current_tool") or ""),
            "live_prompt_tokens": int(live.get("pending_prompt_tokens") or 0),
            "live_completion_tokens": int(live.get("pending_completion_tokens") or 0),
            "live_total_tokens": int(live.get("pending_total_tokens") or 0),
        }
    }

@router.post("/run/{run_id}/stop")
def stop_chat_run(
    run_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = session.exec(
        select(ChatRun).where(ChatRun.run_id == run_id, ChatRun.user_id == user.id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    if str(row.status or "").strip() in {"completed", "error", "stopped"}:
        _clear_run_live_text(run_id)
        return {
            "success": True,
            "run_id": run_id,
            "status": row.status,
            "stop_requested": bool(row.stop_requested),
            "already_finished": True,
        }
    row.stop_requested = True
    now = time.time()
    if str(row.status or "").strip() in {"queued", "running"}:
        row.status = "stopped"
        if row.finished_at is None:
            row.finished_at = now
    row.updated_at = now
    session.add(row)
    session.commit()
    _clear_run_live_text(run_id)
    return {"success": True, "run_id": run_id, "status": row.status, "stop_requested": True}

@router.post("/save", response_model=ChatMessage)
def save_chat_message(
    msg: ChatMessageCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    db_msg = ChatMessage(
        user_id=user.id,
        ai_config_id=msg.ai_config_id,
        ai_kind=msg.ai_kind or "assistant",
        session_id=msg.session_id or "default",
        session_name=msg.session_name,
        role=msg.role,
        content=msg.content,
        think=msg.think,
        tags=msg.tags or "",
        model=msg.model,
        prompt_tokens=msg.prompt_tokens,
        completion_tokens=msg.completion_tokens,
        total_tokens=msg.total_tokens,
        system_prompt=msg.system_prompt,
        finish_reason=msg.finish_reason,
        latency=msg.latency
    )
    session.add(db_msg)
    session.commit()
    session.refresh(db_msg)
    _upsert_session(session, user.id, db_msg.session_id, db_msg.session_name or "未命名会话", db_msg.ai_config_id, db_msg.ai_kind)
    _append_usage_snapshot(
        session=session,
        user_id=user.id,
        ai_config_id=db_msg.ai_config_id,
        ai_kind=db_msg.ai_kind,
        prompt_tokens=db_msg.prompt_tokens or 0,
        completion_tokens=db_msg.completion_tokens or 0,
        total_tokens=db_msg.total_tokens or 0,
    )
    return db_msg

@router.delete("/{msg_id}")
def delete_chat_message(
    msg_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    db_msg = session.get(ChatMessage, msg_id)
    if not db_msg or db_msg.user_id != user.id:
        raise HTTPException(status_code=404, detail="Message not found")
    target_ai_kind = db_msg.ai_kind
    target_ai_config_id = db_msg.ai_config_id
    delete_message_media(session, [db_msg])
    session.delete(db_msg)
    session.commit()
    _rebuild_usage_snapshots(session, user.id, target_ai_kind, target_ai_config_id)
    return {"success": True}

@router.post("/recall/{msg_id}")
def recall_chat_messages(
    msg_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    start_msg = session.get(ChatMessage, msg_id)
    if not start_msg or start_msg.user_id != user.id:
        raise HTTPException(status_code=404, detail="Message not found")
    
    session_id = start_msg.session_id
    start_time = start_msg.created_at
    ai_kind = start_msg.ai_kind
    ai_config_id = start_msg.ai_config_id
    
    statement = select(ChatMessage).where(
        ChatMessage.user_id == user.id,
        ChatMessage.session_id == session_id,
        ChatMessage.ai_kind == ai_kind,
        ChatMessage.created_at >= start_time
    )
    if ai_config_id is not None:
        statement = statement.where(ChatMessage.ai_config_id == ai_config_id)
    messages_to_delete = session.exec(statement).all()
    deleted_count = len(messages_to_delete)
    
    delete_message_media(session, messages_to_delete)
    for msg in messages_to_delete:
        session.delete(msg)
    session.commit()
    _rebuild_usage_snapshots(session, user.id, ai_kind, ai_config_id)
    
    return {"success": True, "deleted_count": deleted_count, "recall_content": start_msg.content}

@router.delete("/clear-all")
def clear_all_messages(
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
    results = session.exec(statement).all()
    delete_message_media(session, results)
    for msg in results:
        session.delete(msg)
    session.commit()
    _rebuild_usage_snapshots(session, user.id, ai_kind, ai_config_id)
    return {"success": True, "count": len(results)}

@router.patch("/{msg_id}/tags")
def update_message_tags(
    msg_id: int,
    update: ChatMessageUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    db_msg = session.get(ChatMessage, msg_id)
    if not db_msg or db_msg.user_id != user.id:
        raise HTTPException(status_code=404, detail="Message not found")
    db_msg.tags = update.tags
    session.add(db_msg)
    session.commit()
    session.refresh(db_msg)
    return db_msg


@router.get("/media/{media_id}/{token}")
def get_chat_message_media(
    media_id: int,
    token: str,
    session: Session = Depends(get_session),
):
    media = get_message_media(session, media_id, token)
    return Response(
        content=media.data,
        media_type=media.media_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Length": str(media.bytes or len(media.data or b"")),
        },
    )

@router.get("/files")
def list_files(
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    project_root = get_project_root(user.id, None)
    all_paths = []
    ignored_dirs = {".git", "__pycache__", "venv", "node_modules", ".aider", ".sandbox_home", ".sandbox_tmp", "_admins"}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [
            d for d in dirs
            if d not in ignored_dirs and not d.startswith(".sandbox")
        ]
        rel_root = os.path.relpath(root, project_root)
        rel_root = "" if rel_root == "." else rel_root
        for directory in dirs:
            all_paths.append((os.path.join(rel_root, directory) if rel_root else directory).replace(os.sep, "/") + "/")
        for filename in files:
            all_paths.append((os.path.join(rel_root, filename) if rel_root else filename).replace(os.sep, "/"))
    return sorted(set(all_paths))


@router.get("/files/read")
def read_workspace_file(
    path: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    full = _safe_user_workspace_path(user.id, path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    size = os.path.getsize(full)
    kind = _workspace_file_kind(os.path.basename(full))
    if size > MAX_WORKSPACE_PREVIEW_BYTES:
        return {
            "path": _workspace_rel_path(user.id, full),
            "size": size,
            "binary": False,
            "too_large": True,
            "content": "",
            "kind": kind,
        }
    with open(full, "rb") as handle:
        data = handle.read()
    if not _is_probably_text(data):
        return {
            "path": _workspace_rel_path(user.id, full),
            "size": size,
            "binary": True,
            "too_large": False,
            "content": "",
            "kind": kind,
        }
    return {
        "path": _workspace_rel_path(user.id, full),
        "size": size,
        "binary": False,
        "too_large": False,
        "content": data.decode("utf-8"),
        "kind": kind,
    }


@router.get("/files/raw")
def raw_workspace_file(
    path: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    full = _safe_user_workspace_path(user.id, path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    media_type, _ = mimetypes.guess_type(full)
    return FileResponse(full, media_type=media_type or "application/octet-stream")


@router.get("/tree")
async def get_tree(
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    result = await registry.call("workspace.run+command", user.id, {"command": "dir /s /b"})
    return {
        "root": ".",
        "tree": result["result"].get("output", ""),
        "command": "dir /s /b",
    }

@router.get("/git-diff")
def get_git_diff(
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    get_current_user(authorization, session)
    raise HTTPException(status_code=410, detail="workspace.git_diff MCP has been removed")

@router.post("/file-content")
def get_file_content(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    filenames = req.get("filenames", [])
    if isinstance(filenames, str):
        filenames = [filenames]
    if not isinstance(filenames, list):
        raise HTTPException(status_code=400, detail="filenames must be a list")
    project_root = get_project_root(user.id, req.get("ai_config_id"))
    out = {}
    for item in filenames[:20]:
        path = str(item or "").strip()
        if not path:
            continue
        full = os.path.abspath(os.path.join(project_root, path))
        if os.path.commonpath([os.path.abspath(project_root), full]) != os.path.abspath(project_root):
            raise HTTPException(status_code=400, detail=f"Unsafe path: {path}")
        if not os.path.isfile(full):
            out[path] = ""
            continue
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            out[path] = handle.read(200_000)
    return out

@router.post("/execute-action")
async def execute_action(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    user = get_current_user(authorization, session)
    
    action = req.get("action")

    if not action:
        raise HTTPException(status_code=400, detail="Missing action field")
        
    try:
        if action in {"edit", "create", "delete"}:
            raise HTTPException(status_code=410, detail="File edit/create/delete actions have been removed. Use workspace.run+command instead.")

        if action == "run":
            result = await registry.call(
                "workspace.run+command",
                user.id,
                {
                    "command": req.get("command"),
                    "cwd": req.get("cwd"),
                    "timeout": req.get("timeout"),
                },
                req.get("ai_config_id"),
            )
            return {
                "success": result["result"]["success"],
                "message": f"Command executed with exit code {result['result']['exit_code']}",
                "output": result["result"]["output"],
                "mcp": result,
            }
            
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stream")
def stream_chat(
    req: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None)
):
    # Ensure user is authenticated
    user = get_current_user(authorization, session)

    messages: List[dict] = req.get("messages", [])
    ai_config_id = req.get("ai_config_id")
    ai_kind = req.get("ai_kind", "assistant")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")

    # KnowledgeBase 文件为真相源：建目录 + 首次导出（幂等）。运行时直接读文件。
    from api.services.knowledge import kb_store

    kb_store.ensure_user_kb(user.id)

    # assistant/core chat both use dedicated AI config
    cfg = None
    if ai_kind in ("assistant", "core"):
        if ai_config_id is None:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user.id,
                )
            ).first()
        else:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.id == ai_config_id,
                    AssistantAIConfig.user_id == user.id,
                )
            ).first()
        if not cfg:
            raise HTTPException(status_code=400, detail="No available assistant AI config")
        api_key, base_url, model = resolve_model_preset(user, cfg)
        # 方案 A：人格 Prompt 直接读 KnowledgeBase/personas/*.md（文件缺失回退 DB）。
        system_prompt = _strip_runtime_injected_sections(kb_store.effective_ai_prompt(user.id, cfg))
        if cfg.database_uri:
            system_prompt = _append_prompt_section(system_prompt, "AI 数据库连接", cfg.database_uri)
    else:
        api_key, base_url, model = resolve_model_preset(user, None)
        system_prompt = _strip_runtime_injected_sections(
            kb_store.effective_system_value(user.id, "admin_prompt")
        )
    if not api_key:
        raise HTTPException(status_code=400, detail="Admin API key not configured")
    if not base_url:
        raise HTTPException(status_code=400, detail="Base URL not configured")
    if not model:
        raise HTTPException(status_code=400, detail="Model not configured")
    if is_cli_base_url(base_url):
        raise HTTPException(status_code=400, detail="CLI 模型不支持此功能，请选择 API 类型的模型预设")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    incoming_system_messages = [
        str(m.get("content", "")).strip()
        for m in messages
        if m.get("role") == "system" and str(m.get("content", "")).strip()
    ]
    merged_system_prompt = system_prompt
    if incoming_system_messages:
        merged_system_prompt = f"{system_prompt}\n\n" + "\n\n".join(incoming_system_messages)

    filtered_messages = [m for m in messages if m.get("role") != "system"]
    filtered_messages.insert(0, {"role": "system", "content": merged_system_prompt})

    payload = {
        "model": model,
        "messages": filtered_messages,
        "stream": True,
        "stream_options": {"include_usage": True} # 请求包含 usage 信息
    }
    warning_template = kb_store.effective_system_value(
        user.id, "mcp_format_error_hint"
    ).strip()

    def generate():
        try:
            response = ai_http_post(base_url, headers=headers, json=payload, stream=True)
            _raise_for_upstream_error(response)
            assistant_text = ""
            for line in response.iter_lines():
                if line:
                    line_str = line.decode("utf-8")
                    if line_str.startswith("data: "):
                        try:
                            chunk = json.loads(line_str[6:])
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            if isinstance(delta, dict):
                                content = delta.get("content")
                                if isinstance(content, str):
                                    assistant_text += content
                                elif isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                                            assistant_text += part["text"]
                        except Exception:
                            pass
                    yield line_str + "\n"
            warning = _build_mcp_stream_warning(assistant_text, cfg, warning_template)
            if warning:
                warn_chunk = {"choices": [{"delta": {"content": f"\n\n{warning}"}}]}
                yield f"data: {json.dumps(warn_chunk, ensure_ascii=False)}\n"
        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
