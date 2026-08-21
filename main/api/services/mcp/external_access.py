"""Credential lifecycle and availability guards for external member MCP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import secrets
import time
from typing import Optional

from sqlmodel import Session, select

from api.models import (
    AssistantAIConfig,
    ExternalMcpCallAudit,
    ExternalMcpCredential,
)


TOKEN_PREFIX = "hsmcp_"
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:+-]{0,254}$")
MAX_ACTIVE_CREDENTIALS_PER_MEMBER = 20


class ExternalMcpAccessError(Exception):
    def __init__(self, message: str, *, code: str, http_status: int = 403):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class ExternalMcpCredentialLimitError(Exception):
    pass


@dataclass(frozen=True)
class ExternalMcpPrincipal:
    credential_id: int
    user_id: int
    ai_config_id: int
    public_id: str


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_credential(
    session: Session,
    cfg: AssistantAIConfig,
    *,
    label: str,
    expires_in_days: Optional[int],
) -> tuple[ExternalMcpCredential, str]:
    ensure_credential_capacity(session, cfg)
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    expires_at = (
        now + int(expires_in_days) * 86400
        if expires_in_days is not None
        else None
    )
    row = ExternalMcpCredential(
        user_id=int(cfg.user_id),
        ai_config_id=int(cfg.id or 0),
        token_hash=token_hash(raw_token),
        token_prefix=raw_token[:12],
        label=label,
        created_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, raw_token


def ensure_credential_capacity(session: Session, cfg: AssistantAIConfig) -> None:
    now = time.time()
    rows = session.exec(
        select(ExternalMcpCredential)
        .where(
            ExternalMcpCredential.user_id == int(cfg.user_id),
            ExternalMcpCredential.ai_config_id == int(cfg.id or 0),
            ExternalMcpCredential.revoked_at == None,  # noqa: E711
            (
                (ExternalMcpCredential.expires_at == None)  # noqa: E711
                | (ExternalMcpCredential.expires_at > now)
            ),
        )
        .order_by(ExternalMcpCredential.created_at.desc())
        .limit(MAX_ACTIVE_CREDENTIALS_PER_MEMBER + 1)
    ).all()
    if len(rows) >= MAX_ACTIVE_CREDENTIALS_PER_MEMBER:
        raise ExternalMcpCredentialLimitError(
            "Too many active external MCP credentials"
        )


def authenticate_credential(
    session: Session,
    authorization: Optional[str],
    *,
    public_id: Optional[str] = None,
) -> tuple[ExternalMcpPrincipal, AssistantAIConfig]:
    token = _bearer_token(authorization)
    row = session.exec(
        select(ExternalMcpCredential).where(
            ExternalMcpCredential.token_hash == token_hash(token)
        )
    ).first()
    now = time.time()
    if row is None or row.revoked_at is not None:
        raise _unauthorized()
    if row.expires_at is not None and float(row.expires_at) <= now:
        raise _unauthorized("Credential expired", code="credential_expired")
    cfg = session.get(AssistantAIConfig, int(row.ai_config_id))
    if cfg is None or int(cfg.user_id) != int(row.user_id):
        raise _unauthorized()
    expected_public_id = str(cfg.external_mcp_public_id or "")
    if public_id is not None and not secrets.compare_digest(public_id, expected_public_id):
        raise _unauthorized()
    if row.last_used_at is None or float(row.last_used_at) <= now - 60:
        row.last_used_at = now
        session.add(row)
        session.commit()
    return ExternalMcpPrincipal(
        credential_id=int(row.id or 0),
        user_id=int(row.user_id),
        ai_config_id=int(row.ai_config_id),
        public_id=expected_public_id,
    ), cfg


def ensure_member_available(cfg: AssistantAIConfig) -> None:
    if not bool(cfg.external_mcp_enabled):
        raise ExternalMcpAccessError(
            "External MCP is disabled for this member",
            code="external_mcp_disabled",
        )
    if not bool(cfg.enabled) or str(cfg.lifecycle_status or "").lower() == "dead":
        raise ExternalMcpAccessError(
            "Member is not available",
            code="member_unavailable",
        )
    if not bool(cfg.mcp_enabled):
        raise ExternalMcpAccessError(
            "MCP is disabled for this member",
            code="member_mcp_disabled",
        )
    if str(cfg.ai_role or "").strip() != "digital_member":
        raise ExternalMcpAccessError(
            "Only digital members can expose MCP tools",
            code="invalid_member_role",
        )


def is_known_external_tool(user_id: int, tool_name: str, view=None) -> bool:
    """Recognize server or configured endpoint tools even when unavailable."""
    name = str(tool_name or "").strip()
    if not _TOOL_NAME_RE.fullmatch(name):
        return False
    if view is not None and (name in view.eligible or name in view.blocked):
        return True
    from mcp_runtime.mcp import registry

    if registry.has(name):
        return True
    from connector_runtime.dispatch.desktop_device_tools import (
        is_endpoint_tool_config_name,
    )

    if is_endpoint_tool_config_name(name):
        return True
    from api.database import engine
    from api.models import DeviceDynamicTool

    with Session(engine) as session:
        return session.exec(
            select(DeviceDynamicTool.id).where(
                DeviceDynamicTool.user_id == int(user_id),
                DeviceDynamicTool.name == name,
            )
        ).first() is not None


def record_audit(
    principal: ExternalMcpPrincipal,
    *,
    method: str,
    tool_name: str = "",
    success: bool,
    error_code: str = "",
    duration_ms: int = 0,
) -> None:
    """Best-effort metadata audit; never persist request or response bodies."""
    from api.database import engine

    try:
        with Session(engine) as session:
            session.add(ExternalMcpCallAudit(
                user_id=principal.user_id,
                ai_config_id=principal.ai_config_id,
                credential_id=principal.credential_id,
                protocol_method=str(method or "")[:40],
                tool_name=str(tool_name or "")[:255],
                success=bool(success),
                error_code=str(error_code or "")[:80],
                duration_ms=max(0, int(duration_ms or 0)),
            ))
            session.commit()
    except Exception:
        pass


def credential_payload(row: ExternalMcpCredential) -> dict:
    now = time.time()
    revoked = row.revoked_at is not None
    expired = row.expires_at is not None and float(row.expires_at) <= now
    state = "revoked" if revoked else "expired" if expired else "active"
    return {
        "id": row.id,
        "label": row.label,
        "token_prefix": row.token_prefix,
        "created_at": _iso_time(row.created_at),
        "expires_at": _iso_time(row.expires_at),
        "last_used_at": _iso_time(row.last_used_at),
        "revoked_at": _iso_time(row.revoked_at),
        "active": not revoked and not expired,
        "state": state,
    }


def _iso_time(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _bearer_token(authorization: Optional[str]) -> str:
    value = str(authorization or "").strip()
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Missing bearer credential", code="missing_credential")
    token = token.strip()
    if len(token) > 512:
        raise _unauthorized()
    return token


def _unauthorized(
    message: str = "Invalid external MCP credential",
    *,
    code: str = "invalid_credential",
) -> ExternalMcpAccessError:
    return ExternalMcpAccessError(message, code=code, http_status=401)
