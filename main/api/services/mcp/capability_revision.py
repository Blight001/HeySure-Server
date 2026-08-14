"""Stable revision calculation for scoped MCP capability views."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def schema_revision(
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    destructive: bool,
    implementation: Mapping[str, Any] | None = None,
) -> str:
    return _digest({
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "destructive": destructive,
        "implementation": implementation,
    })


def capability_revision(
    tools: Mapping[str, Any],
    devices: Iterable[Any],
    *,
    selected_tools: Iterable[str] | None = None,
) -> str:
    payload = {
        "tools": [
            {
                "name": name,
                "schema_version": str(getattr(tool, "schema_version", "") or ""),
                "source": str(getattr(tool, "source_kind", "") or ""),
                "provider": str(getattr(tool, "provider_id", "") or ""),
            }
            for name, tool in sorted(tools.items())
        ],
        "devices": [
            {
                "device_id": str(getattr(item, "device_id", "") or ""),
                "generation": int(getattr(item, "catalog_generation", 0) or 0),
                "catalog_hash": str(getattr(item, "catalog_hash", "") or ""),
            }
            for item in sorted(devices, key=lambda row: str(getattr(row, "device_id", "") or ""))
        ],
        "selected": sorted(str(name) for name in (selected_tools or ()) if str(name)),
    }
    return _digest(payload)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
