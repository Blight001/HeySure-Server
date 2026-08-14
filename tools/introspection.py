import hashlib
import json
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig
from api.devices.presence import online_tool_defs_for_user
_TOOL_NAME_STOP_CHARS = (":", "：", "!", "！")
MCP_INTROSPECTION_TOOLS = {"mcp.describe+tool"}


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
    if not ai_config_id:
        from mcp_runtime.mcp.registry import registry

        return {str(item.get("name") or "").strip() for item in registry.list_tools() if item.get("name")}
    with Session(engine) as session:
        cfg = session.exec(select(AssistantAIConfig).where(
            AssistantAIConfig.id == ai_config_id,
            AssistantAIConfig.user_id == user_id,
        )).first()
    if not cfg or not bool(getattr(cfg, "mcp_enabled", False)):
        return set(MCP_INTROSPECTION_TOOLS)
    from api.services.mcp.capability_view import scoped_tool_view_for_ids

    return set(scoped_tool_view_for_ids(user_id, ai_config_id).eligible_names)


def _describable_tool_names(endpoint_defs: Dict[str, Any]) -> set[str]:
    from mcp_runtime.mcp.registry import registry

    names = {str(item.get("name") or "").strip() for item in registry.list_tools() if item.get("name")}
    names.update(str(name or "").strip() for name in endpoint_defs.keys() if str(name or "").strip())
    return names


def _eligible_tool_names(
    user_id: int,
    ai_config_id: Optional[int],
    available: set[str],
) -> set[str]:
    """Return the describable subset for this AI.

    Calls without an AI config are privileged internal/diagnostic calls and
    retain the legacy full-catalog behaviour. Model-originated calls always
    carry an AI config and are restricted to that config's effective names.
    """
    if ai_config_id is None:
        return set(available)
    allowed = _allowed_tool_names(user_id, ai_config_id)
    resolved = {_resolve_tool_alias(name, available) for name in allowed}
    return {name for name in resolved if name in available}


def _eligibility_revision(eligible: set[str]) -> str:
    encoded = json.dumps(sorted(eligible), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _describe_v2_response(
    *,
    eligible: set[str],
    results: list[Dict[str, Any]],
    errors: list[Dict[str, str]],
    requested_count: int,
    mode: str,
    ai_config_id: Optional[int],
    query: str = "",
    hint: str = "",
    legacy_single: bool = False,
) -> Dict[str, Any]:
    resolved_names = [str(item.get("name") or "").strip() for item in results]
    unresolved = [str(item.get("requested_name") or "") for item in errors]
    session_exposure = ai_config_id is not None
    response: Dict[str, Any] = {
        "schema_version": 2,
        "eligibility_revision": _eligibility_revision(eligible),
        "request": {
            "mode": mode,
            "requested_count": requested_count,
            "resolved_count": len(results),
            "unresolved": unresolved,
        },
        "availability": {
            "eligible_total": len(eligible),
            "returned_count": len(results),
            "eligible_not_returned_count": max(0, len(eligible) - len(set(resolved_names))),
        },
        "exposure": {
            "mode": "next_model_turn" if session_exposure else "not_applicable",
            "callable_next_turn": resolved_names if session_exposure else [],
        },
        "tools": results,
        "errors": errors,
        # One-release compatibility field. New consumers use request.resolved_count.
        "count": len(results),
        "count_semantics": "resolved_requested_tools",
        "hint": hint or (
            "以上 resolved 工具会在下一模型轮挂载，可直接调用；无需为了启用工具创建 Todo。"
            if session_exposure and results
            else "仅返回当前 AI eligible 的工具。"
        ),
    }
    if query:
        response["query"] = query
    # Preserve callers that read result.name/inputSchema for one exact lookup,
    # while making the canonical v2 representation response.tools[0].
    if legacy_single and results:
        response.update(results[0])
        response["legacy_single_fields"] = True
    return response


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
    # mcp.describe+tool, e.g. workspace__search -> workspace.search.
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
        # Editing lives server-side now (device+mcp.manage, library-bound), and
        # only covers desktop/browser device types — there is no device-side
        # manager tool to fall back on anymore.
        device_type = str(spec.get("mcpSource") or "").strip()
        if device_type in ("desktop", "browser"):
            result["implementation_help"] = {
                "inspect": {
                    "tool": "device+mcp.manage",
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


def _parse_describe_request(args: Dict[str, Any]) -> tuple[list[str], str, bool]:
    requested: list[str] = []
    single = str(args.get("tool") or args.get("name") or "").strip()
    if single:
        requested.append(single)
    raw_tools = args.get("tools")
    if isinstance(raw_tools, list):
        requested.extend(str(item).strip() for item in raw_tools if str(item).strip())
    elif isinstance(raw_tools, str) and raw_tools.strip():
        requested.extend(part.strip() for part in raw_tools.split(",") if part.strip())
    return requested, str(args.get("query") or "").strip(), bool(raw_tools) or len(requested) > 1


def _describe_query(
    *,
    query: str,
    eligible: set[str],
    endpoint_defs: Dict[str, Any],
    user_id: int,
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    needle = query.lower()
    eligible_lower = {name.lower() for name in eligible}
    namespaces = {_tool_namespace(name) for name in eligible}
    if needle in namespaces and needle not in eligible_lower:
        example = next((name for name in sorted(eligible) if _tool_namespace(name) == needle), "")
        hint = (
            f"'{query}' 是 MCP 工具的父级 namespace，不是具体工具，不返回内容。"
            "请改用 mcp.describe+tool 指定具体工具名"
            + (f"（如 {example}）" if example else "")
            + "，或用更具体的关键词搜索。"
        )
        matches: list[Dict[str, Any]] = []
    else:
        matches = []
        hint = ""
        for name in sorted(eligible):
            described = _describe_one_tool(name, endpoint_defs, user_id)
            haystack = f"{name} {described.get('description') or ''}".lower()
            if needle in haystack:
                matches.append(described)
            if len(matches) >= 25:
                break
    return _describe_v2_response(
        eligible=eligible,
        results=matches,
        errors=[],
        requested_count=1,
        mode="query",
        ai_config_id=ai_config_id,
        query=query,
        hint=hint,
    )


def _describe_requested(
    *,
    requested: list[str],
    is_batch: bool,
    eligible: set[str],
    endpoint_defs: Dict[str, Any],
    user_id: int,
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    results: list[Dict[str, Any]] = []
    errors: list[Dict[str, str]] = []
    seen_results: set[str] = set()
    seen_errors: set[str] = set()
    for raw in requested:
        resolved = _resolve_tool_alias(raw, eligible)
        if resolved not in eligible:
            if raw not in seen_errors:
                seen_errors.add(raw)
                errors.append({"requested_name": raw, "error": "MCP tool is not available"})
            continue
        if resolved in seen_results:
            continue
        seen_results.add(resolved)
        described = _describe_one_tool(resolved, endpoint_defs, user_id)
        described["requested_name"] = raw
        results.append(described)
    if not is_batch and not results:
        raise HTTPException(status_code=404, detail=f"MCP tool is not available: {requested[0]}")
    return _describe_v2_response(
        eligible=eligible,
        results=results,
        errors=errors,
        requested_count=len(requested),
        mode="batch" if is_batch else "exact",
        ai_config_id=ai_config_id,
        legacy_single=not is_batch,
    )


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
    eligible = _eligible_tool_names(user_id, ai_config_id, available)

    requested, query, is_batch = _parse_describe_request(args)
    if len(requested) > 25:
        raise HTTPException(status_code=400, detail="At most 25 tools may be described per request")
    if query and not requested:
        return _describe_query(
            query=query,
            eligible=eligible,
            endpoint_defs=endpoint_defs,
            user_id=user_id,
            ai_config_id=ai_config_id,
        )
    if not requested:
        raise HTTPException(status_code=400, detail="tool, tools, or query is required")
    return _describe_requested(
        requested=requested,
        is_batch=is_batch,
        eligible=eligible,
        endpoint_defs=endpoint_defs,
        user_id=user_id,
        ai_config_id=ai_config_id,
    )
