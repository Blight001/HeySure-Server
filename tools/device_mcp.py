"""``device+mcp.manage`` — let an AI iterate the device's MCP tools itself.

This is the server-side, AI-callable counterpart to the web console's device
dynamic-tools manager. It reads and writes the same ``DeviceDynamicTool`` store
(scoped by device type) and pushes changes to online devices, so an AI can
author and refine its own tools — e.g. a better file read/write helper — and use
them on the next turn without any human edit or client release.

Desktop tools are JS run on the device with ``(args, cap, ctx)`` in scope, where
``cap`` is the device's native capability library (``cap.call('<id>', args)``).
Browser tools use the safe call/set/return DSL (Chrome MV3 forbids remote JS).
Use ``action="capabilities"`` to discover what a device of each type can run.
"""

from typing import Any, Dict, Optional

from api.devices.live import push_device_dynamic_tools
from api.devices.presence import online_tool_catalog_for_user
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


async def _device_mcp_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()
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
            "enum": ["list", "get", "capabilities", "upsert", "delete", "history", "get_version", "restore", "stats", "failures"],
            "description": (
                "list 列出工具；get 读单个；capabilities 列出该设备类型可调用的原生能力；upsert 创建/修改；delete 删除；"
                "history 查某工具的历史版本；get_version 读某版本完整内容；restore 回滚到指定版本（改坏了用它）；"
                "stats 查各工具调用次数与失败率；failures 查某工具最近失败（含出错的对话 session/run/message 位置），用于追踪并调整。"
            ),
        },
        "device_type": {"type": "string", "enum": ["desktop", "browser"], "description": "目标设备类型。"},
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
    "required": ["action", "device_type"],
    "additionalProperties": False,
}
