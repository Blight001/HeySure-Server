"""Pure AI-facing projection of persisted device capability metadata."""

from __future__ import annotations

import json

from api.models import DevicePresence

from .catalog import normalize_ai_description, prepare_device_catalog


_DEVICE_PURPOSE_DEFAULTS = {
    "desktop": "用于执行本机桌面、文件与系统操作",
    "browser": "用于操作当前浏览器页面和已登录的网站",
    "android": "用于执行已连接 Android 设备上的操作",
    "custom": "用于执行该设备声明并授权的工具",
    "workshop": "用于访问知识工坊能力",
    "toolbox": "用于执行服务端内置工具",
}


def effective_ai_description(row: DevicePresence) -> str:
    override = normalize_ai_description(getattr(row, "ai_description_override", ""))
    reported = normalize_ai_description(getattr(row, "reported_ai_description", ""))
    device_type = str(getattr(row, "device_type", "") or "custom").strip().lower()
    return override or reported or _DEVICE_PURPOSE_DEFAULTS.get(device_type, _DEVICE_PURPOSE_DEFAULTS["custom"])


def device_prompt_metadata(row: DevicePresence, tool_count: int = 0) -> dict:
    return {
        "device_id": str(getattr(row, "device_id", "") or "").strip(),
        "name": str(getattr(row, "name", "") or getattr(row, "device_id", "") or "").strip(),
        "device_type": str(getattr(row, "device_type", "") or "custom").strip() or "custom",
        "purpose": effective_ai_description(row),
        "tool_count": max(0, int(tool_count or 0)),
        "catalog_generation": max(0, int(getattr(row, "catalog_generation", 0) or 0)),
        "catalog_hash": str(getattr(row, "catalog_hash", "") or "").strip(),
    }


def _json_object(value: object) -> dict:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_names(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return sorted({str(item).strip() for item in parsed if str(item).strip()}) if isinstance(parsed, list) else []


def recompute_catalog_hash(row: DevicePresence) -> str:
    # Import lazily because presence also imports the projection helpers above.
    from .presence import mcp_capabilities

    definitions = _json_object(getattr(row, "tool_defs_json", "{}"))
    capabilities = mcp_capabilities(set(_json_names(getattr(row, "capabilities_json", "[]"))))
    prepared = prepare_device_catalog({
        "capabilities": sorted(capabilities),
        "toolDefs": [
            {"name": name, **spec}
            for name, spec in definitions.items()
            if isinstance(spec, dict)
        ],
        "aiDescription": getattr(row, "reported_ai_description", ""),
        "catalogProtocolVersion": getattr(row, "catalog_protocol_version", 1),
    })
    return prepared.catalog_hash
