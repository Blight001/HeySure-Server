"""Transactional persistence for one validated device capability generation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import text
from sqlmodel import Session, select

from api.database import engine
from api.models import DevicePresence

from .catalog import (
    DeviceCatalogError,
    normalize_ai_description,
    prepare_device_catalog,
)


@dataclass(frozen=True)
class PresenceCatalogUpdate:
    user_id: int | None
    device_id: str
    ai_config_id: int | None
    device_type: str
    capabilities: tuple[str, ...]
    tool_defs: Mapping[str, dict]
    online: bool = True
    name: str | None = None
    platform: str | None = None
    icon: str | None = None
    reported_ai_description: str = ""
    catalog_hash: str = ""
    requested_catalog_generation: int | None = None
    catalog_protocol_version: int = 1


def _normalized_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _prepare_legacy_update(update: PresenceCatalogUpdate):
    """Normalize callers that do not already supply an accepted catalog hash."""
    definitions = [
        {"name": str(name), **spec}
        for name, spec in update.tool_defs.items()
        if isinstance(spec, dict)
    ]
    return prepare_device_catalog({
        "capabilities": list(update.capabilities),
        "toolDefs": definitions,
        "aiDescription": update.reported_ai_description,
        "catalogProtocolVersion": update.catalog_protocol_version,
    })


def _accepted_generation(row: DevicePresence, catalog_hash: str, requested: int | None) -> int:
    current_hash = str(getattr(row, "catalog_hash", "") or "")
    current = max(0, int(getattr(row, "catalog_generation", 0) or 0))
    if current_hash and current_hash == catalog_hash:
        return current
    if requested is not None and current:
        if requested < current:
            raise DeviceCatalogError("DEVICE_CATALOG_GENERATION_ROLLBACK", "catalog generation moved backwards")
        if requested == current:
            raise DeviceCatalogError("DEVICE_CATALOG_GENERATION_CONFLICT", "catalog changed without advancing generation")
    return max(current + 1, requested or 0, 1)


def _lock_device(session: Session, device_id: str) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.exec(
            text("SELECT pg_advisory_xact_lock(hashtext(:device_id))"),
            params={"device_id": device_id},
        )


def _latest_rows(session: Session, device_id: str) -> list[DevicePresence]:
    return list(session.exec(
        select(DevicePresence)
        .where(DevicePresence.device_id == device_id)
        .order_by(DevicePresence.updated_at.desc(), DevicePresence.id.desc())
    ).all())


def _apply_display_fields(row: DevicePresence, update: PresenceCatalogUpdate) -> None:
    from .presence import normalize_device_icon

    if update.name is not None:
        row.name = str(update.name or "").strip()
    if update.platform is not None:
        row.platform = str(update.platform or "").strip()
    if update.icon is not None:
        row.icon = normalize_device_icon(update.icon)


def _apply_catalog_fields(
    row: DevicePresence,
    update: PresenceCatalogUpdate,
    capabilities: list[str],
    definitions: Mapping[str, dict],
    catalog_hash: str,
    generation: int,
) -> None:
    user_id = _normalized_int(update.user_id)
    row.user_id = user_id or row.user_id or 0
    row.ai_config_id = _normalized_int(update.ai_config_id)
    row.device_type = str(update.device_type or "").strip()
    row.capabilities_json = json.dumps(capabilities, ensure_ascii=False)
    row.tool_defs_json = json.dumps(definitions, ensure_ascii=False)
    row.reported_ai_description = normalize_ai_description(update.reported_ai_description)
    row.catalog_generation = generation
    row.catalog_hash = catalog_hash
    row.catalog_protocol_version = max(1, int(update.catalog_protocol_version or 1))
    row.online = bool(update.online)
    _apply_display_fields(row, update)
    row.updated_at = time.time()


def swap_presence_catalog(update: PresenceCatalogUpdate) -> dict:
    device_id = str(update.device_id or "").strip()
    if not device_id:
        raise DeviceCatalogError("DEVICE_ID_INVALID", "device id is required")
    catalog_hash = str(update.catalog_hash or "").strip().lower()
    if catalog_hash:
        capabilities = sorted({str(item).strip() for item in update.capabilities if str(item).strip()})
        definitions = dict(update.tool_defs)
    else:
        prepared = _prepare_legacy_update(update)
        capabilities = list(prepared.capabilities)
        definitions = dict(prepared.tool_defs_map)
        catalog_hash = prepared.catalog_hash
    with Session(engine) as session:
        _lock_device(session, device_id)
        rows = _latest_rows(session, device_id)
        row = rows[0] if rows else DevicePresence(device_id=device_id)
        for stale in rows[1:]:
            session.delete(stale)
        if not rows:
            session.add(row)
        user_id = _normalized_int(update.user_id)
        if user_id is not None and row.user_id and row.user_id != user_id:
            raise DeviceCatalogError("DEVICE_CATALOG_OWNER_MISMATCH", "device id belongs to another user")
        generation = _accepted_generation(row, catalog_hash, update.requested_catalog_generation)
        _apply_catalog_fields(row, update, capabilities, definitions, catalog_hash, generation)
        session.commit()
    return {
        "catalog_generation": generation,
        "catalog_hash": catalog_hash,
        "catalog_protocol_version": max(1, int(update.catalog_protocol_version or 1)),
    }
