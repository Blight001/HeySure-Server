"""Pure, redacted diagnostics for scoped tool and device catalog invariants."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from api.devices.presence import recompute_catalog_hash
from api.services.mcp.capability_revision import capability_revision
from api.services.mcp.capability_types import ScopedToolView


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def inspect_scoped_tool_view(view: ScopedToolView) -> dict[str, Any]:
    """Check one immutable view without exposing tool names or schemas."""
    eligible = set(view.eligible_names)
    canonical = {
        name
        for name, capability in view.eligible.items()
        if str(getattr(capability, "canonical_name", "") or "") == name
    }
    routed = set().union(*(set(names) for names in view.device_tool_names.values())) if view.device_tool_names else set()
    recalculated = capability_revision(view.eligible, view.devices)
    problems = []
    if canonical != eligible:
        problems.append("eligible key 与 canonical name 不一致")
    if not routed <= eligible:
        problems.append("设备路由集合包含非 eligible 工具")
    if recalculated != view.revision:
        problems.append("相同输入重算得到不同 capability revision")
    return {
        "ok": not problems,
        "eligible_count": len(eligible),
        "device_count": len(view.devices),
        "routed_tool_count": len(routed),
        "revision": view.revision,
        "problems": problems,
    }


def inspect_exposed_tools(view: ScopedToolView, exposed_names: Iterable[str]) -> dict[str, Any]:
    """Enforce the security invariant ``exposed_tools <= eligible_tools``."""
    exposed = {str(name).strip() for name in exposed_names if str(name).strip()}
    unexpected = exposed - set(view.eligible_names)
    return {
        "ok": not unexpected,
        "exposed_count": len(exposed),
        "ineligible_exposed_count": len(unexpected),
    }


def inspect_described_cache(
    view: ScopedToolView,
    raw_state: object,
    *,
    current_schema_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Classify durable describe entries as restorable, stale, or malformed."""
    state = _decode_described_state(raw_state)
    restorable = 0
    stale = 0
    malformed = 0
    exposed_candidates: set[str] = set()
    for name, item in state.items():
        version = str(item.get("schema_version") or "").strip()
        capability = view.eligible.get(name)
        current_version = (
            str(current_schema_versions.get(name) or "").strip()
            if current_schema_versions is not None
            else str(getattr(capability, "schema_version", "") or "").strip()
        )
        if not version:
            malformed += 1
        elif capability is None or version != current_version:
            stale += 1
        else:
            restorable += 1
            exposed_candidates.add(name)
    surface = inspect_exposed_tools(view, exposed_candidates)
    return {
        "ok": malformed == 0 and surface["ok"],
        "entry_count": len(state),
        "restorable_count": restorable,
        "stale_count": stale,
        "malformed_count": malformed,
        "ineligible_exposed_count": surface["ineligible_exposed_count"],
    }


def inspect_online_device_catalogs(rows: Iterable[Any]) -> dict[str, Any]:
    """Verify that every online presence row has one complete catalog generation."""
    online = [row for row in rows if bool(getattr(row, "online", False))]
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for row in online:
        device_id = str(getattr(row, "device_id", "") or "").strip()
        if device_id in seen:
            duplicate_ids.add(device_id)
        seen.add(device_id)
        reasons = _device_catalog_problems(row)
        if reasons:
            invalid.append({"device_id": device_id, "reasons": reasons})
    if duplicate_ids:
        for device_id in sorted(duplicate_ids):
            invalid.append({"device_id": device_id, "reasons": ["duplicate_active_presence"]})
    invalid_ids = {str(item["device_id"]) for item in invalid}
    return {
        "ok": not invalid,
        "online_count": len(online),
        "valid_count": sum(
            1 for row in online
            if str(getattr(row, "device_id", "") or "").strip() not in invalid_ids
        ),
        "invalid_count": len(invalid),
        "invalid": invalid[:10],
    }


def _decode_described_state(raw_state: object) -> dict[str, Mapping[str, Any]]:
    if isinstance(raw_state, Mapping):
        parsed = raw_state
    else:
        try:
            parsed = json.loads(str(raw_state or "{}"))
        except (TypeError, ValueError):
            return {"<malformed-json>": {}}
    if not isinstance(parsed, Mapping):
        return {"<malformed-json>": {}}
    return {
        str(name): item if isinstance(item, Mapping) else {}
        for name, item in parsed.items()
        if str(name).strip()
    }


def _device_catalog_problems(row: Any) -> list[str]:
    problems: list[str] = []
    generation = int(getattr(row, "catalog_generation", 0) or 0)
    protocol = int(getattr(row, "catalog_protocol_version", 0) or 0)
    persisted_hash = str(getattr(row, "catalog_hash", "") or "").strip().lower()
    if generation < 1:
        problems.append("generation_not_active")
    if protocol < 1:
        problems.append("protocol_invalid")
    if not _SHA256_RE.fullmatch(persisted_hash):
        problems.append("hash_format_invalid")
        return problems
    try:
        if recompute_catalog_hash(row) != persisted_hash:
            problems.append("hash_mismatch")
    except Exception:
        problems.append("catalog_unreadable")
    return problems
