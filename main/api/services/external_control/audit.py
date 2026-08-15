"""Sanitized audit persistence for external controllers."""

import json
from typing import Any, Optional

from sqlmodel import select

from api.models.external_control import ExternalControllerCredential, ExternalControllerEvent


SENSITIVE_KEYS = {"api_key", "password", "token", "cookie", "secret", "authorization"}


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).lower().replace("-", "_")
            out[str(key)] = "[redacted]" if any(part in normalized for part in SENSITIVE_KEYS) else _safe(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:20_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:20_000]


class ExternalAuditMixin:
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
        payload = json.dumps(_safe(result if result is not None else {}), ensure_ascii=False, separators=(",", ":"))[:50_000]
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
                "result": json.loads(row.result_json or "{}"),
                "created_at": row.created_at,
            }
            for row in rows
        ]
