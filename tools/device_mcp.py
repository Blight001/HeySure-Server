"""``device+mcp.manage`` — device inventory, MCP scope and dynamic-tool governance.

Besides the server-side counterpart to the web console's dynamic-tools manager,
this tool exposes user-owned device IDs and the same per-device MCP allow-list
used by the Workshop permission editor.  An AI can therefore discover the exact
``device_id`` needed by ``member.manage`` and prepare a device's tool scope
without creating a second source of truth.

Desktop tools are JS run on the device with ``(args, cap, ctx)`` in scope, where
``cap`` is the device's native capability library (``cap.call('<id>', args)``).
Browser tools use the safe call/set/return DSL (Chrome MV3 forbids remote JS).
Use ``action="capabilities"`` to discover what a device of each type can run.
"""

from typing import Any, Dict, Optional

from api.devices.live import connected_agent_rows_for_user, emit_agent_list_for_user, push_device_dynamic_tools
from api.devices.mcp_permissions import get_scope, set_scope
from api.devices.presence import mcp_capabilities, online_tool_catalog_for_user, tool_defs_for_agent
from api.services.device_tools import device_workspace_tools as dyn


def _capabilities(user_id: int, device_type: str) -> list:
    out: Dict[str, str] = {}
    for device in online_tool_catalog_for_user(user_id):
        if str(device.get("device_type") or "") != device_type:
            continue
        for tool in device.get("tools") or []:
            name = str(tool.get("name") or "").strip()
            if name:
                out.setdefault(name, str(tool.get("description") or "").strip())
    return [{"name": name, "description": out[name]} for name in sorted(out)]


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _error(code: str, message: str, next_step: str, **details: Any) -> Dict[str, Any]:
    """Return one predictable, recovery-oriented error shape for small models."""
    return {
        "ok": False,
        "error": message,
        "code": code,
        "next_step": next_step,
        **details,
    }


def _device_type(row: Dict[str, Any]) -> str:
    if row.get("isToolbox"):
        return "toolbox"
    if row.get("isWorkshop"):
        return "workshop"
    if row.get("isBrowserExtension"):
        return "browser"
    if row.get("isWindowsDesktop"):
        return "desktop"
    if row.get("isAndroid"):
        return "android"
    return str(row.get("deviceType") or row.get("device_type") or "custom").strip().lower() or "custom"


def _device_capabilities(row: Dict[str, Any]) -> list[str]:
    raw = row.get("capabilities")
    if not isinstance(raw, (list, tuple, set)):
        raw = []
    return sorted(mcp_capabilities({str(item).strip() for item in raw if str(item).strip()}))


def _device_rows(user_id: int, ai_config_id: Optional[int] = None) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in connected_agent_rows_for_user(user_id):
        row = raw if isinstance(raw, dict) else {}
        device_id = str(row.get("id") or row.get("deviceId") or row.get("device_id") or "").strip()
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        capabilities = _device_capabilities(row)
        saved_scope = get_scope(user_id, device_id, ai_config_id)
        allowed = capabilities if saved_scope is None else sorted(set(capabilities) & saved_scope)
        online = bool(row.get("online", row.get("lifecycle") != "offline"))
        bound_ids = row.get("boundAiConfigIds")
        if not isinstance(bound_ids, list):
            bound_ids = []
        ai_config_id = _positive_int(row.get("aiConfigId") or row.get("ai_config_id"))
        rows.append({
            "deviceId": device_id,
            # Explicit alias for Chinese prompts that ask for the 设备号. The
            # identifier is intentionally a string (linux-..., browser-..., etc.).
            "deviceNumber": device_id,
            "name": str(row.get("remark") or row.get("name") or device_id).strip() or device_id,
            "registeredName": str(row.get("name") or "").strip(),
            "deviceType": _device_type(row),
            "platform": str(row.get("platform") or "").strip(),
            "online": online,
            "aiConfigId": ai_config_id,
            "boundAiConfigIds": sorted({item for item in (_positive_int(value) for value in bound_ids) if item}),
            "availableMcpCount": len(capabilities),
            "allowedMcpCount": len(allowed),
        })
    rows.sort(key=lambda item: (not item["online"], item["deviceType"], item["name"], item["deviceId"]))
    return rows


def _owned_device(
    user_id: int,
    device_id: Any,
    ai_config_id: Optional[int] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    wanted = str(device_id or "").strip()
    if not wanted:
        return None, _error(
            "DEVICE_ID_REQUIRED",
            "device_id is required",
            "Call action=devices to obtain a deviceId, then retry with device_id=<deviceId>.",
        )
    for summary in _device_rows(user_id, ai_config_id):
        if summary["deviceId"] == wanted:
            return summary, None
    return None, _error(
        "DEVICE_NOT_FOUND",
        f"device not found or not owned by current user: {wanted}",
        "Call action=devices without device_id, choose a returned deviceId, and retry.",
        supplied_device_id=wanted,
    )


def _scope_payload(user_id: int, summary: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    device_id = summary["deviceId"]
    source = next(
        (
            row for row in connected_agent_rows_for_user(user_id)
            if str((row or {}).get("id") or (row or {}).get("deviceId") or "").strip() == device_id
        ),
        {},
    )
    capabilities = _device_capabilities(source if isinstance(source, dict) else {})
    saved_scope = get_scope(user_id, device_id, ai_config_id)
    allowed = capabilities if saved_scope is None else sorted(set(capabilities) & saved_scope)
    defs = source.get("toolDefs") if isinstance(source, dict) else None
    if not isinstance(defs, dict):
        defs = tool_defs_for_agent(user_id, device_id)
    tools = []
    for name in capabilities:
        spec = defs.get(name) if isinstance(defs.get(name), dict) else {}
        schema = spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else spec.get("inputSchema")
        tools.append({
            "name": name,
            "allowed": name in allowed,
            "description": str(spec.get("description") or "").strip(),
            "inputSchema": schema if isinstance(schema, dict) else {},
            "destructive": bool(spec.get("destructive")),
        })
    return {
        "ok": True,
        "device": summary,
        "deviceId": device_id,
        "deviceNumber": device_id,
        "capabilities": capabilities,
        "allowed": allowed,
        "hasRecord": saved_scope is not None,
        "tools": tools,
    }


async def _device_mcp_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()

    if action == "devices":
        rows = _device_rows(user_id, ai_config_id)
        available_total = len(rows)
        requested_id = str(args.get("device_id") or "").strip()
        if requested_id:
            rows = [row for row in rows if row["deviceId"] == requested_id]
            if not rows:
                return _error(
                    "DEVICE_NOT_FOUND",
                    f"device not found or not owned by current user: {requested_id}",
                    "Remove device_id to list available devices, then retry with a returned deviceId.",
                    supplied_device_id=requested_id,
                )

        if "online" in args:
            wanted_online = args.get("online")
            if not isinstance(wanted_online, bool):
                return _error(
                    "INVALID_ONLINE_FILTER",
                    "online must be true or false",
                    "Pass a JSON boolean, for example online=true; do not pass the string 'true'.",
                )
            rows = [row for row in rows if row["online"] is wanted_online]

        device_kind = str(args.get("device_kind") or "").strip().lower()
        if device_kind:
            rows = [row for row in rows if row["deviceType"] == device_kind]

        bound_ai_config_id = _positive_int(args.get("bound_ai_config_id"))
        if args.get("bound_ai_config_id") is not None and bound_ai_config_id is None:
            return _error(
                "INVALID_BOUND_AI_CONFIG_ID",
                "bound_ai_config_id must be a positive integer",
                "Pass the member ai_config_id returned by member.manage list/get.",
            )
        if bound_ai_config_id:
            rows = [
                row for row in rows
                if bound_ai_config_id == row.get("aiConfigId")
                or bound_ai_config_id in row.get("boundAiConfigIds", [])
            ]

        query = str(args.get("query") or "").strip().casefold()
        if query:
            rows = [
                row for row in rows
                if query in " ".join((
                    row["deviceId"], row["name"], row["registeredName"],
                    row["deviceType"], row["platform"],
                )).casefold()
            ]

        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 0
        if limit < 1 or limit > 200:
            return _error(
                "INVALID_LIMIT",
                "limit must be between 1 and 200",
                "Retry with limit=20 (default) or another integer from 1 to 200.",
            )
        matched_total = len(rows)
        rows = rows[:limit]
        return {
            "ok": True,
            "count": len(rows),
            "matchedTotal": matched_total,
            "availableTotal": available_total,
            "truncated": matched_total > len(rows),
            "devices": rows,
            "bindingHint": "Use a returned deviceId/deviceNumber in member.manage create/update device_ids.",
            "nextStep": "Refine with online, device_kind, bound_ai_config_id, query, or device_id when multiple devices match.",
        }

    if action in {"scope_get", "scope_set"}:
        summary, error = _owned_device(user_id, args.get("device_id"), ai_config_id)
        if error:
            return error
        assert summary is not None
        if action == "scope_get":
            return _scope_payload(user_id, summary, ai_config_id)

        requested = args.get("tools")
        if not isinstance(requested, list):
            return {"ok": False, "error": "tools array is required for scope_set (use [] to disable all)"}
        if any(not isinstance(item, str) or not item.strip() for item in requested):
            return {"ok": False, "error": "tools must contain non-empty MCP tool-name strings"}
        requested_names = {item.strip() for item in requested}
        current = _scope_payload(user_id, summary, ai_config_id)
        capabilities = set(current["capabilities"])
        unknown = sorted(requested_names - capabilities)
        if unknown:
            return {
                "ok": False,
                "error": "scope contains tools not reported by this device",
                "unknownTools": unknown,
                "capabilities": sorted(capabilities),
            }
        stored = set_scope(
            user_id,
            summary["deviceId"],
            requested_names,
            ai_config_id=ai_config_id,
            device_type=summary.get("deviceType") or "",
        )
        if stored is None:
            return {"ok": False, "error": "failed to save device MCP scope"}
        await emit_agent_list_for_user(user_id)
        return _scope_payload(user_id, summary, ai_config_id)

    try:
        device_type = dyn.normalize_device_type(args.get("device_type"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    name = str(args.get("name") or "").strip()

    if action == "list":
        tools = dyn.list_tools(user_id, device_type)
        return {
            "ok": True,
            "deviceType": device_type,
            "tools": [
                {"name": t["name"], "description": t["description"], "code_kind": t["code_kind"], "enabled": t["enabled"], "status": t.get("status", "active")}
                for t in tools
            ],
        }
    if action == "capabilities":
        return {"ok": True, "deviceType": device_type, "capabilities": _capabilities(user_id, device_type)}
    if action == "get":
        tool = dyn.get_tool(user_id, device_type, name)
        if not tool:
            return {"ok": False, "error": f"tool not found: {name}"}
        return {"ok": True, "tool": tool}
    if action == "stats":
        from api.services.mcp import mcp_stats

        tool_names = [t["name"] for t in dyn.list_tools(user_id, device_type)]
        return {"ok": True, "deviceType": device_type, "stats": mcp_stats.tool_stats(user_id, tool_names)}
    if action == "failures":
        from api.services.mcp import mcp_stats

        return {"ok": True, "name": name, "failures": mcp_stats.recent_failures(user_id, name)}
    if action == "history":
        return {"ok": True, "name": name, "versions": dyn.list_versions(user_id, device_type, name)}
    if action == "get_version":
        snapshot = dyn.get_version(user_id, device_type, int(args.get("version_id") or 0))
        if snapshot is None:
            return {"ok": False, "error": "version not found"}
        return {"ok": True, "version": snapshot}
    if action == "restore":
        tool = dyn.restore_version(
            user_id, device_type, int(args.get("version_id") or 0),
            actor="ai", ai_config_id=ai_config_id,
        )
        if tool is None:
            return {"ok": False, "error": "version not found"}
        reached = await push_device_dynamic_tools(user_id, device_type)
        return {"ok": True, "action": "restore", "tool": tool, "pushedToDevices": reached}
    if action == "delete":
        if not dyn.delete_tool(user_id, device_type, name, actor="ai", ai_config_id=ai_config_id):
            return {"ok": False, "error": f"tool not found: {name}"}
        reached = await push_device_dynamic_tools(user_id, device_type)
        return {"ok": True, "action": "delete", "name": name, "pushedToDevices": reached}
    if action == "upsert":
        definition = args.get("definition")
        if not isinstance(definition, dict):
            return {"ok": False, "error": "definition object is required for upsert"}
        try:
            tool = dyn.upsert_tool(
                user_id, device_type, definition,
                enabled=bool(args.get("enabled", True)), actor="ai", ai_config_id=ai_config_id,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        reached = await push_device_dynamic_tools(user_id, device_type)
        note = None
        if tool.get("status") == "draft":
            note = "工具已保存为 draft（草稿），需用户在网页端批准为 active 后才会下发到设备并可调用。"
        return {"ok": True, "action": "upsert", "tool": tool, "pushedToDevices": reached, "note": note}

    return {"ok": False, "error": f"unsupported action: {action}"}


DEVICE_MCP_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["devices", "scope_get", "scope_set", "list", "get", "capabilities", "upsert", "delete", "history", "get_version", "restore", "stats", "failures"],
            "description": (
                "devices 列出账号下设备并返回 deviceId/deviceNumber（可交给 member.manage 的 device_ids 绑定成员）；"
                "scope_get 读取某台设备的 MCP 使用范围；scope_set 保存精确允许范围（tools=[] 表示全不选）；"
                "list 列出动态工具；get 读单个；capabilities 列出该设备类型可调用的原生能力；upsert 创建/修改；delete 删除；"
                "history 查某工具的历史版本；get_version 读某版本完整内容；restore 回滚到指定版本（改坏了用它）；"
                "stats 查各工具调用次数与失败率；failures 查某工具最近失败（含出错的对话 session/run/message 位置），用于追踪并调整。"
            ),
        },
        "device_id": {"type": "string", "description": "设备号/设备 ID。scope_get、scope_set 必填；devices 可选用于精确查询。"},
        "online": {"type": "boolean", "description": "devices：按在线状态过滤；true 仅在线，false 仅离线。必须传 JSON 布尔值。"},
        "device_kind": {"type": "string", "description": "devices：按返回的 deviceType 精确过滤，如 desktop、browser、custom、toolbox、workshop。"},
        "bound_ai_config_id": {"type": "integer", "minimum": 1, "description": "devices：只返回已绑定到该数字成员 ai_config_id 的设备。"},
        "query": {"type": "string", "description": "devices：按设备 ID、名称、类型或平台做不区分大小写的包含匹配。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20, "description": "devices：最多返回条数，默认 20；truncated=true 表示仍有匹配项。"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "scope_set 的完整 MCP 允许名单；传 [] 表示全不选。"},
        "device_type": {"type": "string", "enum": ["desktop", "browser"], "description": "动态工具动作（list/get/capabilities/upsert/delete/history/get_version/restore/stats/failures）的目标设备类型。"},
        "name": {"type": "string", "description": "工具名（get/delete/history 必填；upsert 也可放在 definition.name）。"},
        "version_id": {"type": "integer", "description": "get_version / restore 的目标版本号（来自 history）。"},
        "enabled": {"type": "boolean", "description": "upsert 时是否启用（默认 true）。"},
        "definition": {
            "type": "object",
            "description": "upsert 的完整定义。",
            "properties": {
                "name": {"type": "string", "description": "工具名，如 fs.read_better；与现有同名则覆盖。"},
                "description": {"type": "string", "description": "给 AI 看的工具说明。"},
                "input_schema": {"type": "object", "description": "JSON Schema 入参定义。"},
                "code_kind": {"type": "string", "enum": ["js", "program", "runtime"], "description": "desktop 用 js；browser 用 program；runtime 用设备运行时执行 source。缺省按 runtime/js 推断。"},
                "js": {"type": "string", "description": "desktop：函数体，作用域有 args/cap/ctx，用 return 返回。例：return await cap.call('fs.read', args)。"},
                "code": {"type": "array", "description": "browser：call/set/return 指令数组（1-32 条）。", "items": {"type": "object"}},
                "runtime": {"type": "string", "enum": ["python", "powershell", "shell"], "description": "设备运行时（仅 desktop）。设置后改用 source 提供源码。"},
                "source": {"type": "string", "description": "runtime 源码：python 脚本（用 args 取参、result 返回）/ powershell 脚本 / shell 命令，支持 ${args.x} 模板。"},
                "permissions": {"type": "array", "items": {"type": "string"}, "description": "runtime 工具声明的权限标签（如 shell.write、filesystem.read），设备按策略 allow/confirm/deny。"},
            },
            "required": ["name", "description", "input_schema"],
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}
