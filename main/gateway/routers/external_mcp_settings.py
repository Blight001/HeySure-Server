"""Authenticated AI-setting endpoints for external member MCP sharing."""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from api.database import get_session
from api.core.settings import settings
from api.models import ExternalMcpCredential
from api.services.access.access_guards import get_ai_config_or_404
from api.services.mcp.external_access import (
    ExternalMcpCredentialLimitError,
    create_credential,
    credential_payload,
)
from gateway.routers.auth import get_current_user
from gateway.routers.auth import _normalize_public_url


router = APIRouter()
PREFIX = "/api/ai"
_CANONICAL_ENDPOINT = "/mcp/member"
_MAX_LISTED_CREDENTIALS = 100


class ExternalMcpToggleRequest(BaseModel):
    enabled: bool


class ExternalMcpCredentialRequest(BaseModel):
    label: str = Field(default="External AI", min_length=1, max_length=80)
    expires_in_days: Optional[int] = Field(default=90, ge=1, le=3650)


@router.get("/configs/{config_id}/external-mcp")
def get_external_mcp_settings(
    config_id: int,
    request: Request,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    base_url = _external_base_url(request)
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    credentials = _credentials_for(session, user.id, config_id)
    return _settings_payload(cfg, credentials, base_url)


@router.put("/configs/{config_id}/external-mcp")
def update_external_mcp_settings(
    config_id: int,
    body: ExternalMcpToggleRequest,
    request: Request,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    base_url = _external_base_url(request)
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    if str(cfg.ai_role or "").strip() != "digital_member":
        raise HTTPException(status_code=400, detail="Only digital members can expose MCP tools")
    cfg.external_mcp_enabled = body.enabled
    cfg.updated_at = time.time()
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _settings_payload(
        cfg,
        _credentials_for(session, user.id, config_id),
        base_url,
    )


@router.post("/configs/{config_id}/external-mcp/credentials", status_code=201)
def issue_external_mcp_credential(
    config_id: int,
    body: ExternalMcpCredentialRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    base_url = _external_base_url(request)
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    if str(cfg.ai_role or "").strip() != "digital_member":
        raise HTTPException(status_code=400, detail="Only digital members can expose MCP tools")
    label = " ".join(body.label.split())
    if not label:
        raise HTTPException(status_code=422, detail="Credential label is required")
    try:
        row, token = create_credential(
            session,
            cfg,
            label=label,
            expires_in_days=body.expires_in_days,
        )
    except ExternalMcpCredentialLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    endpoint = f"{base_url}{_CANONICAL_ENDPOINT}"
    return {
        "credential": credential_payload(row),
        "token": token,
        "endpoint": endpoint,
        "member_endpoint": f"{base_url}/mcp/members/{cfg.external_mcp_public_id}",
        "codex_config": _codex_config(endpoint),
    }


@router.delete("/configs/{config_id}/external-mcp/credentials/{credential_id}")
def revoke_external_mcp_credential(
    config_id: int,
    credential_id: int,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(None),
):
    user = get_current_user(authorization, session)
    get_ai_config_or_404(session, config_id, user.id)
    row = session.exec(
        select(ExternalMcpCredential).where(
            ExternalMcpCredential.id == credential_id,
            ExternalMcpCredential.user_id == user.id,
            ExternalMcpCredential.ai_config_id == config_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="External MCP credential not found")
    if row.revoked_at is None:
        row.revoked_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
    return {"revoked": True, "credential": credential_payload(row)}


def _credentials_for(session: Session, user_id: int, config_id: int):
    return session.exec(
        select(ExternalMcpCredential)
        .where(
            ExternalMcpCredential.user_id == user_id,
            ExternalMcpCredential.ai_config_id == config_id,
        )
        .order_by(ExternalMcpCredential.created_at.desc())
        .limit(_MAX_LISTED_CREDENTIALS)
    ).all()


def _settings_payload(cfg, credentials, base_url: str) -> dict:
    tool_count, revision = _capability_summary(cfg)
    return {
        "ai_config_id": cfg.id,
        "enabled": bool(cfg.external_mcp_enabled),
        "public_id": cfg.external_mcp_public_id,
        "endpoint": f"{base_url}{_CANONICAL_ENDPOINT}",
        "member_endpoint": f"{base_url}/mcp/members/{cfg.external_mcp_public_id}",
        "tool_count": tool_count,
        "capability_revision": revision,
        "credentials": [credential_payload(row) for row in credentials],
    }


def _external_base_url(request: Request) -> str:
    configured = _normalize_public_url(settings.public_base_url)
    if configured:
        return configured
    # Host/Forwarded headers are not trusted here. Only direct loopback
    # development may infer an address; public deployments must configure it.
    if request.url.hostname in {"localhost", "127.0.0.1", "::1"}:
        return str(request.base_url).rstrip("/")
    raise HTTPException(
        status_code=503,
        detail="HEYSURE_PUBLIC_BASE_URL is required for external MCP on public hosts",
    )


def _capability_summary(cfg) -> tuple[int, str]:
    from api.services.mcp.capability_view import scoped_tool_view_for_ids

    view = scoped_tool_view_for_ids(int(cfg.user_id), int(cfg.id))
    return len(view.eligible), view.revision


def _codex_config(endpoint: str) -> str:
    from api.services.mcp.external_transport import codex_tool_timeout_seconds

    return (
        "[mcp_servers.heysure_member]\n"
        f'url = "{endpoint}"\n'
        'bearer_token_env_var = "HEYSURE_MEMBER_MCP_TOKEN"\n'
        f"tool_timeout_sec = {codex_tool_timeout_seconds()}"
    )
