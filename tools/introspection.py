import hashlib
import json
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig
from api.devices.presence import online_tool_defs_for_user
_TOOL_NAME_STOP_CHARS = (":", "：", "!", "！")
MCP_INTROSPECTION_TOOLS = {"mcp.describe_tool"}


def _with_schema_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    version_source = {
        "name": payload.get("name"),
        "description": payload.get("description"),
        "inputSchema": payload.get("inputSchema"),
        "destructive": payload.get("destructive"),
        "implementation": payload.get("implementation"),
    }
    encoded = json.dumps(
        version_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload["schemaVersion"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return payload


def _tool_namespace(name: str) -> str:
    if "." in name:
        return name.split(".", 1)[0]
    if "_" in name:
        return name.split("_", 1)[0]
    return "other"


def _allowed_tool_names(user_id: int, ai_config_id: Optional[int]) -> set[str]:
    from connector_runtime.dispatch.desktop_device_tools import (
        endpoint_bridge_tools_for_config,
        endpoint_tools_for_config,
        strip_endpoint_tool_config_names,
    )
    from api.services.tasks.task_system import with_workspace_read_by_name_compat
    import json

    allowed: set[str] = set(MCP_INTROSPECTION_TOOLS)
    if not ai_config_id:
        from mcp_runtime.mcp.registry import registry

        return {str(item.get("name") or "").strip() for item in registry.list_tools() if item.get("name")}

    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == ai_config_id,
                AssistantAIConfig.user_id == user_id,
            )
        ).first()
    if not cfg or not cfg.mcp_enabled:
        return allowed
    try:
        parsed = json.loads(cfg.mcp_tools or "[]")
        if isinstance(parsed, list):
            allowed.update(str(item).strip() for item in parsed if isinstance(item, str) and str(item).strip())
    except Exception:
        pass
    allowed = strip_endpoint_tool_config_names(allowed)
    allowed = with_workspace_read_by_name_compat(allowed)
    allowed.update(endpoint_bridge_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
    allowed.update(endpoint_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
    return allowed


def _describable_tool_names(endpoint_defs: Dict[str, Any]) -> set[str]:
    from mcp_runtime.mcp.registry import registry

    names = {str(item.get("name") or "").strip() for item in registry.list_tools() if item.get("name")}
    names.update(str(name or "").strip() for name in endpoint_defs.keys() if str(name or "").strip())
    return names


def _workshop_tool_defs() -> Dict[str, Dict[str, Any]]:
    """Definitions for built-in workshop tools.

    Workshop tools are executed through the endpoint dispatch path, but their
    schemas live in the built-in workshop catalog rather than the MCP registry.
    Keep describe_tool able to explain them even before the presence snapshot is
    refreshed for this process.
    """
    try:
        from library import engine as workshop_engine

        return workshop_engine.tool_defs_map()
    except Exception:
        return {}


def _resolve_tool_alias(name: str, available: set[str]) -> str:
    raw = str(name or "").strip()
    if raw in available:
        return raw

    # Models sometimes copy a full catalog line back into describe_tool, e.g.
    # "browser/browser_screenshot !: 对当前标签页截图...". Be forgiving and
    # recover the concrete tool name before checking permissions.
    candidates: list[str] = []
    text = raw.lstrip("-*• \t`").strip()
    if text:
        candidates.append(text)
        for stop in _TOOL_NAME_STOP_CHARS:
            idx = text.find(stop)
            if idx > 0:
                candidates.append(text[:idx].strip())
        head = text.split(None, 1)[0].strip()
        if head:
            candidates.append(head)

    for candidate in candidates:
        clean = candidate.strip().strip("`'\"，,;；")
        if clean in available:
            return clean
        if "/" in clean:
            suffix = clean.rsplit("/", 1)[-1].strip()
            if suffix in available:
                return suffix
        if "." in clean:
            suffix = clean.split(".", 1)[-1].strip()
            if suffix in available:
                return suffix
            underscored = clean.replace(".", "_")
            if underscored in available:
                return underscored
        if "__" in clean:
            dotted = clean.replace("__", ".")
            if dotted in available:
                return dotted
            underscored = clean.replace("__", "_")
            if underscored in available:
                return underscored

    # Native tool schemas replace characters outside [a-zA-Z0-9_-] with "__".
    # Accept that form here so models can pass the visible native name back to
    # mcp.describe_tool, e.g. workspace__search -> workspace.search.
    if "__" in raw:
        dotted = raw.replace("__", ".")
        if dotted in available:
            return dotted
        underscored = raw.replace("__", "_")
        if underscored in available:
            return underscored
    if "." in raw:
        suffix = raw.split(".", 1)[-1].strip()
        if suffix in available:
            return suffix
        underscored = raw.replace(".", "_")
        if underscored in available:
            return underscored
    return raw


def _describe_one_tool(name: str, endpoint_defs: Dict[str, Any], user_id: int = 0) -> Dict[str, Any]:
    from mcp_runtime.mcp.registry import registry
    from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool

    if name in endpoint_defs or is_endpoint_agent_tool(name):
        spec = endpoint_defs.get(name) or {}
        result = {
            "name": name,
            "description": str(spec.get("description") or "").strip(),
            "inputSchema": spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {},
            "destructive": bool(spec.get("destructive", True)),
            "implementation": spec.get("implementation") if isinstance(spec.get("implementation"), dict) else {},
        }
        # Editing lives server-side now (device_mcp.manage, library-bound), and
        # only covers desktop/browser device types — there is no device-side
        # manager tool to fall back on anymore.
        device_type = str(spec.get("mcpSource") or "").strip()
        if device_type in ("desktop", "browser"):
            result["implementation_help"] = {
                "inspect": {
                    "tool": "device_mcp.manage",
                    "arguments": {"action": "get", "device_type": device_type, "name": name},
                },
                "note": "Call get to read the stored definition before editing via upsert (requires library binding).",
            }
        return _with_schema_version(result)
    tool = registry.get(name)
    description = str(tool.description or "").strip()
    input_schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    # 文件为真相源：KnowledgeBase/mcp/*.md 的描述与参数说明优先于注册表原文。
    if user_id:
        try:
            from api.services.knowledge.librarian_service import intrinsic_input_schema, intrinsic_tool_description

            description = intrinsic_tool_description(int(user_id), tool.name, description)
            input_schema = intrinsic_input_schema(int(user_id), tool.name, input_schema)
        except Exception:
            pass
    return _with_schema_version({
        "name": tool.name,
        "description": description,
        "inputSchema": input_schema,
        "destructive": tool.destructive,
    })


def current_tool_schema_versions(user_id: int, names: Iterable[str]) -> Dict[str, str]:
    """Resolve current effective versions using the same source as describe_tool."""
    endpoint_defs = online_tool_defs_for_user(user_id)
    endpoint_defs.update(
        {name: spec for name, spec in _workshop_tool_defs().items() if name not in endpoint_defs}
    )
    available = _describable_tool_names(endpoint_defs)
    versions: Dict[str, str] = {}
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or name not in available:
            continue
        try:
            versions[name] = str(_describe_one_tool(name, endpoint_defs, user_id).get("schemaVersion") or "")
        except Exception:
            continue
    return versions


def _mcp_describe_tool(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int] = None):
    """Load full schema(s) for one or more allowed tools.

    Supports three input shapes so the model can load everything it needs in a
    single round-trip:
    - ``tool``/``name``: one tool (backward-compatible single-object result).
    - ``tools``: a list (or comma-separated string) of exact tool names.
    - ``query``: keyword search across tool names + descriptions.
    """

    endpoint_defs = online_tool_defs_for_user(user_id)
    endpoint_defs.update(
        {name: spec for name, spec in _workshop_tool_defs().items() if name not in endpoint_defs}
    )
    available = _describable_tool_names(endpoint_defs)

    requested: list[str] = []
    single = str(args.get("tool") or args.get("name") or "").strip()
    if single:
        requested.append(single)
    raw_tools = args.get("tools")
    if isinstance(raw_tools, list):
        requested.extend(str(item).strip() for item in raw_tools if str(item).strip())
    elif isinstance(raw_tools, str) and raw_tools.strip():
        requested.extend(part.strip() for part in raw_tools.split(",") if part.strip())

    query = str(args.get("query") or "").strip()
    is_batch = bool(raw_tools) or (len(requested) > 1)

    # Keyword search mode: return schemas for every known tool that matches.
    # describe_tool is an introspection helper, so it does not apply the
    # execution allow-list; actual tool execution still checks permissions.
    if query and not requested:
        needle = query.lower()
        # A bare parent namespace ("browser", "workspace", …) is not a real tool
        # — it is only the first segment shared by many tools. Searching it used
        # to dump every tool under that namespace (e.g. all browser_* schemas) in
        # one shot. Return no content and point at the concrete-tool path instead,
        # so the parent name itself never surfaces a payload.
        available_lower = {name.lower() for name in available}
        namespaces = {_tool_namespace(name) for name in available}
        if needle in namespaces and needle not in available_lower:
            example = next((name for name in sorted(available) if _tool_namespace(name) == needle), "")
            hint = (
                f"'{query}' 是 MCP 工具的父级 namespace，不是具体工具，不返回内容。"
                "请改用 mcp.describe_tool 指定具体工具名"
                + (f"（如 {example}）" if example else "")
                + "，或用更具体的关键词搜索。"
            )
            return {"query": query, "count": 0, "tools": [], "hint": hint}
        matches: list[Dict[str, Any]] = []
        for name in sorted(available):
            described = _describe_one_tool(name, endpoint_defs, user_id)
            haystack = f"{name} {described.get('description') or ''}".lower()
            if needle in haystack:
                matches.append(described)
            if len(matches) >= 25:
                break
        return {"query": query, "count": len(matches), "tools": matches}

    if not requested:
        raise HTTPException(status_code=400, detail="tool, tools, or query is required")

    results: list[Dict[str, Any]] = []
    errors: list[Dict[str, str]] = []
    seen_results: set[str] = set()
    seen_errors: set[str] = set()
    for raw in requested:
        resolved = _resolve_tool_alias(raw, available)
        if resolved not in available:
            if raw in seen_errors:
                continue
            seen_errors.add(raw)
            errors.append({"requested_name": raw, "error": "Unknown MCP tool"})
            continue
        if resolved in seen_results:
            continue
        seen_results.add(resolved)
        described = _describe_one_tool(resolved, endpoint_defs, user_id)
        described["requested_name"] = raw
        results.append(described)

    # Backward-compatible single-object result for one plain tool lookup.
    if not is_batch:
        if results:
            return results[0]
        raise HTTPException(status_code=404, detail=f"Unknown MCP tool: {requested[0]}")

    return {"count": len(results), "tools": results, "errors": errors}
