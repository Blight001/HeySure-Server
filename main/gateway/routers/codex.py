"""Web API for sessions backed by a local Windows Codex app-server bridge."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import get_session
from api.models import AssistantAIConfig, DevicePresence
from api.runtime.internal_http import internal_post
from api.services.codex_bridge import append_event, create_session, events_for_chat_session, events_for_session, get_session as get_codex_session, models_for_user, sessions_for_user
from .auth import get_current_user

router = APIRouter()
PREFIX = "/api/codex"


class CodexSessionCreate(BaseModel):
    deviceId: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    cwd: str = Field(default="", max_length=2048)
    aiConfigId: Optional[int] = None
    sandboxPolicy: str = Field(default="", max_length=80)
    approvalPolicy: str = Field(default="", max_length=80)


class CodexRunRequest(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _connector_required() -> str:
    base = str(settings.connector_runtime_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Codex bridge requires connector runtime")
    return base


@router.get("/models")
def codex_models(session: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, session)
    return {"models": models_for_user(user.id)}


@router.get("/sessions")
def codex_sessions(session: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, session)
    return {"sessions": sessions_for_user(user.id)}


@router.post("/sessions")
async def codex_session_create(payload: CodexSessionCreate, session: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, session)
    device_id = payload.deviceId.strip()
    if payload.aiConfigId is not None:
        config = session.exec(select(AssistantAIConfig).where(
            AssistantAIConfig.id == payload.aiConfigId,
            AssistantAIConfig.user_id == user.id,
        )).first()
        if config is None:
            raise HTTPException(status_code=404, detail="AI config not found")
    presence = session.exec(select(DevicePresence).where(DevicePresence.user_id == user.id, DevicePresence.device_id == device_id)).first()
    if not presence or not presence.online:
        raise HTTPException(status_code=409, detail="Codex device is offline")
    allowed = {str(item.get("id")) for item in models_for_user(user.id) if str(item.get("deviceId")) == device_id}
    if payload.model not in allowed:
        raise HTTPException(status_code=400, detail="model is not advertised by this device")
    row = create_session(user_id=user.id, device_id=device_id, model=payload.model, cwd=payload.cwd, ai_config_id=payload.aiConfigId, sandbox_policy=payload.sandboxPolicy, approval_policy=payload.approvalPolicy)
    try:
        result = await internal_post(_connector_required() + "/", "/internal/codex/start", json={"session_id": row.session_id, "user_id": user.id, "device_id": device_id, "model": payload.model, "cwd": payload.cwd, "sandbox_policy": payload.sandboxPolicy, "approval_policy": payload.approvalPolicy})
    except Exception as exc:
        append_event(user_id=user.id, session_id=row.session_id, event_type="session.failed", payload={"error": "bridge unavailable"})
        raise HTTPException(status_code=503, detail="Codex bridge unavailable") from exc
    return {"sessionId": row.session_id, "status": "starting", "bridge": result}


@router.post("/sessions/{session_id}/run")
async def codex_run(session_id: str, payload: CodexRunRequest, db: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, db)
    if get_codex_session(user.id, session_id) is None:
        raise HTTPException(status_code=404, detail="Codex session not found")
    append_event(user_id=user.id, session_id=session_id, event_type="user.input", payload={"input": payload.input, "metadata": payload.metadata})
    try:
        result = await internal_post(_connector_required() + "/", "/internal/codex/run", json={"session_id": session_id, "user_id": user.id, "input": payload.input, "metadata": payload.metadata})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Codex bridge unavailable") from exc
    return {"ok": True, "bridge": result}


@router.post("/sessions/{session_id}/turn")
async def codex_turn(session_id: str, payload: CodexRunRequest, db: Session = Depends(get_session), authorization: str = Header(None)):
    return await codex_run(session_id, payload, db, authorization)


@router.post("/sessions/{session_id}/interrupt")
async def codex_interrupt(session_id: str, db: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, db)
    if get_codex_session(user.id, session_id) is None:
        raise HTTPException(status_code=404, detail="Codex session not found")
    try:
        result = await internal_post(_connector_required() + "/", "/internal/codex/interrupt", json={"session_id": session_id, "user_id": user.id})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Codex bridge unavailable") from exc
    return {"ok": True, "bridge": result}


@router.get("/sessions/{session_id}/events")
def codex_events(session_id: str, after: int = 0, db: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, db)
    if get_codex_session(user.id, session_id) is None:
        raise HTTPException(status_code=404, detail="Codex session not found")
    return {"sessionId": session_id, "events": events_for_session(user_id=user.id, session_id=session_id, after=after)}


@router.get("/chat-sessions/{chat_session_id}/events")
def codex_chat_events(chat_session_id: str, db: Session = Depends(get_session), authorization: str = Header(None)):
    user = get_current_user(authorization, db)
    return {"chatSessionId": chat_session_id,
            "events": events_for_chat_session(user_id=user.id, chat_session_id=chat_session_id)}

