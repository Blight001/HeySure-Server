"""Security, context snapshots and explicit run transitions for remote control."""

import hashlib
import json
import secrets
import time
import uuid
from typing import Any, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import AssistantAIConfig, DevicePresence, User
from api.models.external_control import (
    ExternalControllerCredential,
    ExternalControllerEvent,
    ExternalControllerRun,
)
from .state import RunTransitionError, TERMINAL_RUN_STATES, transition_run


SCOPES = {"context:read", "mcp:call", "run:write", "audit:read"}
SENSITIVE_KEYS = {"api_key", "password", "token", "cookie", "secret", "authorization"}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).lower().replace("-", "_")
            out[str(key)] = "[redacted]" if any(part in normalized for part in SENSITIVE_KEYS) else _safe_value(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:20_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:20_000]


class ExternalControlService:
    def __init__(self, session: Session):
        self.session = session

    def get_member(self, user_id: int, ai_config_id: int) -> AssistantAIConfig:
        cfg = self.session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == ai_config_id,
                AssistantAIConfig.user_id == user_id,
            )
        ).first()
        if not cfg:
            raise HTTPException(status_code=404, detail="AI config not found")
        if cfg.execution_mode != "external_mcp":
            raise HTTPException(status_code=409, detail="AI execution mode is not external MCP")
        return cfg

    def issue_credential(self, user_id: int, ai_config_id: int, label: str, ttl_days: int) -> Tuple[ExternalControllerCredential, str]:
        self.get_member(user_id, ai_config_id)
        now = time.time()
        existing = self.session.exec(
            select(ExternalControllerCredential).where(
                ExternalControllerCredential.user_id == user_id,
                ExternalControllerCredential.ai_config_id == ai_config_id,
                ExternalControllerCredential.state == "active",
            )
        ).all()
        for row in existing:
            row.state = "revoked"
            row.revoked_at = now
            self.session.add(row)
        token = f"hsc_{secrets.token_urlsafe(36)}"
        credential = ExternalControllerCredential(
            user_id=user_id,
            ai_config_id=ai_config_id,
            token_hash=_token_hash(token),
            token_prefix=token[:12],
            label=str(label or "Codex")[:80],
            scopes_json=json.dumps(sorted(SCOPES)),
            expires_at=now + max(1, min(int(ttl_days), 90)) * 86400,
        )
        self.session.add(credential)
        self.session.commit()
        self.session.refresh(credential)
        self.add_event(credential, "credential.issued", result={"label": credential.label, "expires_at": credential.expires_at})
        return credential, token

    def authenticate(self, authorization: Optional[str], required_scope: Optional[str] = None) -> Tuple[ExternalControllerCredential, User, AssistantAIConfig]:
        scheme, _, token = str(authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail="Missing controller bearer token")
        credential = self.session.exec(
            select(ExternalControllerCredential).where(
                ExternalControllerCredential.token_hash == _token_hash(token.strip())
            )
        ).first()
        now = time.time()
        if not credential or credential.state != "active":
            raise HTTPException(status_code=401, detail="Controller credential is not active")
        if credential.expires_at <= now:
            credential.state = "expired"
            self.session.add(credential)
            self.session.commit()
            raise HTTPException(status_code=401, detail="Controller credential has expired")
        scopes = set(_json_loads(credential.scopes_json, []))
        if required_scope and required_scope not in scopes:
            raise HTTPException(status_code=403, detail="Controller scope is not permitted")
        user = self.session.get(User, credential.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="Controller owner no longer exists")
        cfg = self.get_member(credential.user_id, credential.ai_config_id)
        credential.last_seen_at = now
        self.session.add(credential)
        self.session.commit()
        return credential, user, cfg

    @staticmethod
    def require_scope(credential: ExternalControllerCredential, scope: str) -> None:
        scopes = set(_json_loads(credential.scopes_json, []))
        if scope not in scopes:
            raise HTTPException(status_code=403, detail="Controller scope is not permitted")

    def revoke(self, user_id: int, ai_config_id: int, credential_id: Optional[int] = None) -> int:
        statement = select(ExternalControllerCredential).where(
            ExternalControllerCredential.user_id == user_id,
            ExternalControllerCredential.ai_config_id == ai_config_id,
            ExternalControllerCredential.state == "active",
        )
        if credential_id is not None:
            statement = statement.where(ExternalControllerCredential.id == credential_id)
        rows = self.session.exec(statement).all()
        now = time.time()
        for row in rows:
            row.state = "revoked"
            row.revoked_at = now
            self.session.add(row)
        self.session.commit()
        return len(rows)

    def context_snapshot(self, credential: ExternalControllerCredential, cfg: AssistantAIConfig) -> dict:
        from api.services.knowledge import kb_store

        devices = self.session.exec(
            select(DevicePresence).where(
                DevicePresence.user_id == credential.user_id,
                DevicePresence.ai_config_id == credential.ai_config_id,
            ).order_by(DevicePresence.updated_at.desc())
        ).all()
        return {
            "member": {
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "role": cfg.digital_member_role,
                "management_scope": cfg.management_scope,
                "execution_mode": cfg.execution_mode,
                "prompt": kb_store.effective_ai_prompt(credential.user_id, cfg),
            },
            "devices": [self._device_payload(row) for row in devices],
            "configured_mcp_tools": _json_loads(cfg.mcp_tools, []),
            "controller": {
                "credential_id": credential.id,
                "scopes": _json_loads(credential.scopes_json, []),
                "expires_at": credential.expires_at,
            },
        }

    @staticmethod
    def _device_payload(row: DevicePresence) -> dict:
        return {
            "device_id": row.device_id,
            "type": row.device_type,
            "name": row.remark or row.name,
            "platform": row.platform,
            "online": row.online,
            "capabilities": _json_loads(row.capabilities_json, []),
            "updated_at": row.updated_at,
        }

    def add_event(
        self,
        credential: ExternalControllerCredential,
        event_type: str,
        *,
        run_id: Optional[str] = None,
        tool_name: str = "",
        status: str = "ok",
        result: Any = None,
    ) -> ExternalControllerEvent:
        safe = _safe_value(result if result is not None else {})
        payload = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))[:50_000]
        row = ExternalControllerEvent(
            user_id=credential.user_id,
            ai_config_id=credential.ai_config_id,
            credential_id=credential.id,
            run_id=run_id,
            event_type=event_type[:80],
            tool_name=tool_name[:200],
            status=status[:40],
            result_json=payload,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def start_run(self, credential: ExternalControllerCredential, title: str, lease_seconds: int = 300) -> ExternalControllerRun:
        now = time.time()
        row = ExternalControllerRun(
            run_id=f"xrun_{uuid.uuid4().hex}",
            user_id=credential.user_id,
            ai_config_id=credential.ai_config_id,
            credential_id=int(credential.id or 0),
            title=str(title or "External MCP run")[:300],
        )
        transition_run(row, "leased", now)
        row.lease_owner = credential.token_prefix
        row.lease_expires_at = now + max(30, min(int(lease_seconds), 1800))
        transition_run(row, "running", now)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.add_event(credential, "run.started", run_id=row.run_id, result={"title": row.title})
        return row

    def finish_run(self, credential: ExternalControllerCredential, run_id: str, status: str, summary: str = "", error: str = "") -> ExternalControllerRun:
        row = self.session.exec(
            select(ExternalControllerRun).where(
                ExternalControllerRun.run_id == run_id,
                ExternalControllerRun.credential_id == credential.id,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Controller run not found")
        target = str(status or "succeeded").lower()
        if target not in TERMINAL_RUN_STATES:
            raise HTTPException(status_code=400, detail="Invalid terminal run status")
        try:
            transition_run(row, target)
        except RunTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        row.summary = str(summary or "")[:20_000]
        row.error_message = str(error or "")[:4_000]
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.add_event(credential, "run.finished", run_id=row.run_id, status=target, result={"summary": row.summary, "error": row.error_message})
        return row

    def list_events(self, user_id: int, ai_config_id: int, limit: int = 100) -> list[dict]:
        rows = self.session.exec(
            select(ExternalControllerEvent).where(
                ExternalControllerEvent.user_id == user_id,
                ExternalControllerEvent.ai_config_id == ai_config_id,
            ).order_by(ExternalControllerEvent.created_at.desc()).limit(max(1, min(limit, 500)))
        ).all()
        return [
            {
                "id": row.id,
                "run_id": row.run_id,
                "event_type": row.event_type,
                "tool_name": row.tool_name,
                "status": row.status,
                "result": _json_loads(row.result_json, {}),
                "created_at": row.created_at,
            }
            for row in rows
        ]
