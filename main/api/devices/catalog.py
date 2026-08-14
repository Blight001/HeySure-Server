"""Validation and canonical hashing for one endpoint capability generation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MAX_TOOLS = 256
MAX_TOOL_NAME_CHARS = 160
MAX_DESCRIPTION_CHARS = 240
MAX_TOOL_DESCRIPTION_CHARS = 2_000
MAX_SCHEMA_BYTES = 64 * 1024
MAX_CATALOG_BYTES = 512 * 1024

_WHITESPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/-]{8,}|(?:token|cookie|password|secret|authorization)\s*[:=])"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@")
_INSTRUCTION_RE = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior)|system\s+prompt|"
    r"忽略(?:以上|之前|先前|所有)?(?:规则|指令|提示词)|系统提示词|伪造(?:工具)?(?:结果|回执))"
)


class DeviceCatalogError(ValueError):
    """Stable validation failure safe to expose to a device client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedDeviceCatalog:
    capabilities: tuple[str, ...]
    tool_defs: tuple[dict[str, Any], ...]
    tool_defs_map: Mapping[str, dict[str, Any]]
    reported_ai_description: str
    requested_generation: int | None
    protocol_version: int
    catalog_hash: str


def _normalize_description(value: object, max_chars: int) -> str:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()[:max_chars]
    if _SECRET_RE.search(cleaned) or _URL_CREDENTIAL_RE.search(cleaned) or _INSTRUCTION_RE.search(cleaned):
        return ""
    return cleaned


def normalize_ai_description(value: object) -> str:
    """Return safe, single-line descriptive metadata or an empty fallback."""
    return _normalize_description(value, MAX_DESCRIPTION_CHARS)


def _normalized_tool_name(value: object) -> str:
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not name
        or len(name) > MAX_TOOL_NAME_CHARS
        or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in name)
    ):
        raise DeviceCatalogError("DEVICE_CATALOG_TOOL_NAME_INVALID", "catalog contains an invalid tool name")
    return name


def _json_size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise DeviceCatalogError("DEVICE_CATALOG_SCHEMA_INVALID", "catalog must be JSON serializable") from exc


def _normalize_capabilities(values: Sequence[object]) -> tuple[str, ...]:
    if len(values) > MAX_TOOLS:
        raise DeviceCatalogError("DEVICE_CATALOG_TOO_MANY_TOOLS", f"catalog may contain at most {MAX_TOOLS} tools")
    names = [_normalized_tool_name(value) for value in values]
    if len(names) != len(set(names)):
        raise DeviceCatalogError("DEVICE_CATALOG_DUPLICATE_TOOL", "catalog contains duplicate tool names")
    return tuple(sorted(names))


def _normalize_tool_defs(values: Sequence[object], allowed: set[str]) -> tuple[dict[str, Any], ...]:
    if len(values) > MAX_TOOLS:
        raise DeviceCatalogError("DEVICE_CATALOG_TOO_MANY_DEFS", f"catalog may contain at most {MAX_TOOLS} definitions")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise DeviceCatalogError("DEVICE_CATALOG_DEFINITION_INVALID", "every tool definition must be an object")
        name = _normalized_tool_name(value.get("name"))
        if name in seen:
            raise DeviceCatalogError("DEVICE_CATALOG_DUPLICATE_DEFINITION", "catalog contains duplicate definitions")
        if name not in allowed:
            raise DeviceCatalogError("DEVICE_CATALOG_DEFINITION_ORPHANED", "tool definition is not listed in capabilities")
        seen.add(name)
        schema = value.get("input_schema")
        if not isinstance(schema, dict):
            schema = value.get("inputSchema")
        if not isinstance(schema, dict):
            raise DeviceCatalogError("DEVICE_CATALOG_SCHEMA_INVALID", "tool input schema must be an object")
        if _json_size(schema) > MAX_SCHEMA_BYTES:
            raise DeviceCatalogError("DEVICE_CATALOG_SCHEMA_TOO_LARGE", "one tool schema exceeds the size limit")
        description = _normalize_description(value.get("description"), MAX_TOOL_DESCRIPTION_CHARS)
        implementation = value.get("implementation") if isinstance(value.get("implementation"), dict) else {}
        permissions = value.get("permissions") if isinstance(value.get("permissions"), list) else []
        out.append({
            "name": name,
            "description": description,
            "input_schema": schema,
            "destructive": bool(value.get("destructive")),
            "implementation": implementation,
            "permissions": sorted({str(item).strip() for item in permissions if str(item).strip()}),
        })
    return tuple(sorted(out, key=lambda item: item["name"]))


def prepare_device_catalog(info: Mapping[str, Any]) -> PreparedDeviceCatalog:
    capabilities_raw = info.get("capabilities") or []
    tool_defs_raw = info.get("toolDefs") or []
    if not isinstance(capabilities_raw, list) or not isinstance(tool_defs_raw, list):
        raise DeviceCatalogError("DEVICE_CATALOG_INVALID", "capabilities and toolDefs must be arrays")
    capabilities = _normalize_capabilities(capabilities_raw)
    tool_defs = _normalize_tool_defs(tool_defs_raw, set(capabilities))
    reported_description = normalize_ai_description(info.get("aiDescription"))
    generation_raw = info.get("catalogGeneration")
    try:
        requested_generation = None if generation_raw is None else int(generation_raw)
    except (TypeError, ValueError) as exc:
        raise DeviceCatalogError("DEVICE_CATALOG_GENERATION_INVALID", "catalogGeneration must be an integer") from exc
    if requested_generation is not None and not 0 <= requested_generation < 2**63:
        raise DeviceCatalogError("DEVICE_CATALOG_GENERATION_INVALID", "catalogGeneration is outside the valid range")
    try:
        protocol_version = int(info.get("catalogProtocolVersion") or 1)
    except (TypeError, ValueError) as exc:
        raise DeviceCatalogError("DEVICE_CATALOG_PROTOCOL_INVALID", "catalogProtocolVersion must be an integer") from exc
    if not 1 <= protocol_version <= 100:
        raise DeviceCatalogError("DEVICE_CATALOG_PROTOCOL_INVALID", "catalogProtocolVersion is unsupported")
    canonical = {
        "capabilities": capabilities,
        "tool_defs": tool_defs,
        "reported_ai_description": reported_description,
        "protocol_version": protocol_version,
    }
    if _json_size(canonical) > MAX_CATALOG_BYTES:
        raise DeviceCatalogError("DEVICE_CATALOG_TOO_LARGE", "catalog exceeds the total size limit")
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    defs_map = {item["name"]: {key: val for key, val in item.items() if key != "name"} for item in tool_defs}
    return PreparedDeviceCatalog(
        capabilities=capabilities,
        tool_defs=tool_defs,
        tool_defs_map=defs_map,
        reported_ai_description=reported_description,
        requested_generation=requested_generation,
        protocol_version=protocol_version,
        catalog_hash=hashlib.sha256(payload).hexdigest(),
    )
