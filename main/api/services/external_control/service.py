"""Security, context snapshots and explicit run transitions for remote control."""

import hashlib
import json
import secrets
import time
import uuid
from typing import Any, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from api.models import AssistantAIConfig, ChatMessage, ChatMessageCreate, DevicePresence, User
from api.models.external_control import (
    ExternalControllerCredential,
    ExternalControllerRun,
    ExternalControllerTurn,
)
from api.services.mcp.capability_view import scoped_tool_view_for_ids
from api.services.chat.chat_persistence import _save_message
from .audit import ExternalAuditMixin
from .state import (
    RunTransitionError,
    TERMINAL_RUN_STATES,
    TurnTransitionError,
    transition_run,
    transition_turn,
)


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


class ExternalControlService(ExternalAuditMixin):
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
            "configured_mcp_tools": sorted(
                scoped_tool_view_for_ids(credential.user_id, cfg.id).eligible_names
            ),
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

    def enqueue_message(
        self,
        user_id: int,
        ai_config_id: int,
        *,
        content: str,
        session_id: str,
        session_name: str,
        ai_kind: str = "assistant",
        tags: str = "",
    ) -> ExternalControllerTurn:
        """Persist a user message and queue it without starting the AI runtime."""
        self.get_member(user_id, ai_config_id)
        body = str(content or "").strip()
        if not body:
            raise HTTPException(status_code=400, detail="Message content is required")
        message = _save_message(
            self.session,
            user_id,
            ChatMessageCreate(
                role="user",
                content=body,
                tags=str(tags or ""),
                ai_config_id=ai_config_id,
                ai_kind=str(ai_kind or "assistant"),
                session_id=str(session_id or "default"),
                session_name=str(session_name or "未命名会话"),
            ),
        )
        now = time.time()
        turn = ExternalControllerTurn(
            turn_id=f"xturn_{uuid.uuid4().hex}",
            user_id=user_id,
            ai_config_id=ai_config_id,
            user_message_id=int(message.id or 0),
            session_id=message.session_id,
            session_name=message.session_name or session_name or "未命名会话",
            ai_kind=message.ai_kind,
            created_at=now,
            updated_at=now,
        )
        self.session.add(turn)
        self.session.commit()
        self.session.refresh(turn)
        return turn

    def _recover_expired_turns(self, credential: ExternalControllerCredential) -> None:
        now = time.time()
        rows = self.session.exec(
            select(ExternalControllerTurn).where(
                ExternalControllerTurn.user_id == credential.user_id,
                ExternalControllerTurn.ai_config_id == credential.ai_config_id,
                ExternalControllerTurn.status == "running",
                ExternalControllerTurn.lease_expires_at.is_not(None),
                ExternalControllerTurn.lease_expires_at <= now,
            )
        ).all()
        for row in rows:
            if row.attempt >= 3:
                transition_turn(row, "failed", now)
                row.error_message = "controller lease expired after maximum attempts"
            else:
                transition_turn(row, "queued", now)
            self.session.add(row)
        if rows:
            self.session.commit()

    def claim_message(
        self,
        credential: ExternalControllerCredential,
        *,
        turn_id: str = "",
        lease_seconds: int = 300,
        history_limit: int = 30,
    ) -> Optional[dict]:
        self._recover_expired_turns(credential)
        statement = select(ExternalControllerTurn).where(
            ExternalControllerTurn.user_id == credential.user_id,
            ExternalControllerTurn.ai_config_id == credential.ai_config_id,
            ExternalControllerTurn.status == "queued",
        )
        wanted = str(turn_id or "").strip()
        if wanted:
            statement = statement.where(ExternalControllerTurn.turn_id == wanted)
        statement = statement.order_by(ExternalControllerTurn.created_at.asc()).with_for_update(skip_locked=True)
        row = self.session.exec(statement).first()
        if row is None:
            return None
        now = time.time()
        transition_turn(row, "running", now)
        row.credential_id = credential.id
        row.lease_owner = credential.token_prefix
        row.lease_expires_at = now + max(30, min(int(lease_seconds), 1800))
        row.attempt += 1
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.add_event(
            credential,
            "conversation.claimed",
            status="ok",
            result={"turn_id": row.turn_id, "session_id": row.session_id, "attempt": row.attempt},
        )
        return self._turn_payload(row, history_limit=history_limit, include_history=True)

    def renew_message(
        self, credential: ExternalControllerCredential, turn_id: str, lease_seconds: int = 300
    ) -> dict:
        row = self._owned_running_turn(credential, turn_id)
        row.lease_expires_at = time.time() + max(30, min(int(lease_seconds), 1800))
        row.updated_at = time.time()
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return self._turn_payload(row)

    def reply_message(
        self,
        credential: ExternalControllerCredential,
        turn_id: str,
        content: str,
        *,
        think: str = "",
        model: str = "external-codex",
    ) -> dict:
        row = self._owned_running_turn(credential, turn_id)
        body = str(content or "").strip()
        if not body:
            raise HTTPException(status_code=400, detail="Reply content is required")
        message = _save_message(
            self.session,
            credential.user_id,
            ChatMessageCreate(
                role="assistant",
                content=body,
                think=str(think or "") or None,
                model=str(model or "external-codex")[:200],
                finish_reason="stop",
                ai_config_id=row.ai_config_id,
                ai_kind=row.ai_kind,
                session_id=row.session_id,
                session_name=row.session_name,
            ),
        )
        transition_turn(row, "succeeded")
        row.assistant_message_id = int(message.id or 0)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.add_event(
            credential,
            "conversation.replied",
            status="ok",
            result={"turn_id": row.turn_id, "session_id": row.session_id, "message_id": message.id},
        )
        return self._turn_payload(row)

    def fail_message(
        self, credential: ExternalControllerCredential, turn_id: str, error: str
    ) -> dict:
        row = self._owned_running_turn(credential, turn_id)
        transition_turn(row, "failed")
        row.error_message = str(error or "external controller failed")[:4_000]
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        self.add_event(
            credential,
            "conversation.failed",
            status="failed",
            result={"turn_id": row.turn_id, "error": row.error_message},
        )
        return self._turn_payload(row)

    def list_messages(
        self,
        credential: ExternalControllerCredential,
        *,
        status: str = "queued",
        limit: int = 20,
    ) -> list[dict]:
        self._recover_expired_turns(credential)
        statement = select(ExternalControllerTurn).where(
            ExternalControllerTurn.user_id == credential.user_id,
            ExternalControllerTurn.ai_config_id == credential.ai_config_id,
        )
        wanted = str(status or "").strip().lower()
        if wanted:
            statement = statement.where(ExternalControllerTurn.status == wanted)
        rows = self.session.exec(
            statement.order_by(ExternalControllerTurn.created_at.asc()).limit(max(1, min(int(limit), 100)))
        ).all()
        return [self._turn_payload(row) for row in rows]

    def list_messages_for_owner(
        self, user_id: int, ai_config_id: int, *, status: str = "", limit: int = 100
    ) -> list[dict]:
        self.get_member(user_id, ai_config_id)
        statement = select(ExternalControllerTurn).where(
            ExternalControllerTurn.user_id == user_id,
            ExternalControllerTurn.ai_config_id == ai_config_id,
        )
        wanted = str(status or "").strip().lower()
        if wanted:
            statement = statement.where(ExternalControllerTurn.status == wanted)
        rows = self.session.exec(
            statement.order_by(ExternalControllerTurn.created_at.desc()).limit(max(1, min(int(limit), 100)))
        ).all()
        return [self._turn_payload(row) for row in rows]

    def turn_payload_for_owner(self, row: ExternalControllerTurn) -> dict:
        return self._turn_payload(row)

    def _owned_running_turn(
        self, credential: ExternalControllerCredential, turn_id: str
    ) -> ExternalControllerTurn:
        row = self.session.exec(
            select(ExternalControllerTurn).where(
                ExternalControllerTurn.turn_id == str(turn_id or "").strip(),
                ExternalControllerTurn.user_id == credential.user_id,
                ExternalControllerTurn.ai_config_id == credential.ai_config_id,
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Conversation turn not found")
        if row.status != "running" or row.credential_id != credential.id:
            raise HTTPException(status_code=409, detail="Conversation turn is not owned by this controller")
        if row.lease_expires_at is not None and row.lease_expires_at <= time.time():
            raise HTTPException(status_code=409, detail="Conversation turn lease has expired")
        return row

    def _turn_payload(
        self,
        row: ExternalControllerTurn,
        *,
        history_limit: int = 30,
        include_history: bool = False,
    ) -> dict:
        user_message = self.session.get(ChatMessage, row.user_message_id)
        payload = {
            "turn_id": row.turn_id,
            "status": row.status,
            "session_id": row.session_id,
            "session_name": row.session_name,
            "ai_kind": row.ai_kind,
            "user_message_id": row.user_message_id,
            "content": str(user_message.content if user_message else ""),
            "attempt": row.attempt,
            "lease_expires_at": row.lease_expires_at,
            "assistant_message_id": row.assistant_message_id,
            "error": row.error_message,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        if include_history:
            rows = self.session.exec(
                select(ChatMessage).where(
                    ChatMessage.user_id == row.user_id,
                    ChatMessage.ai_config_id == row.ai_config_id,
                    ChatMessage.ai_kind == row.ai_kind,
                    ChatMessage.session_id == row.session_id,
                    ChatMessage.id <= row.user_message_id,
                ).order_by(ChatMessage.id.desc()).limit(max(1, min(int(history_limit), 100)))
            ).all()
            payload["history"] = [
                {"id": item.id, "role": item.role, "content": item.content, "created_at": item.created_at}
                for item in reversed(rows)
            ]
        return payload
