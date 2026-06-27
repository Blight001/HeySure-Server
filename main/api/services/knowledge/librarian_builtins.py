"""librarian_builtins — 内置条目 + 固有属性 + 固有人格 + 系统提示词。"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from ...database import engine
from ...models import AssistantAIConfig, User
from . import kb_store
import logging

from .librarian_core import (
    _BUILTIN_ENTRIES,
    _BUILTIN_UPDATED_AT,
    _INTRINSIC_PROPERTIES_OVERRIDES_FILE,
    _kb_root,
)
from .librarian_thoughts import _inheritance_thoughts_payload, _render_inheritance_thoughts_body

logger = logging.getLogger(__name__)


def _builtin_entries(*, user_id: Optional[int] = None, with_body: bool = False) -> List[Dict[str, Any]]:
    return [
        item
        for memory_id in (
            "builtin.intrinsic_personas",
            "builtin.system_prompts",
            "builtin.inheritance_skills",
            "builtin.inheritance_tools",
        )
        if (item := _builtin_entry(memory_id, user_id=user_id, with_body=with_body)) is not None
    ]


def _builtin_entry(memory_id: str, *, user_id: Optional[int] = None, with_body: bool = False) -> Optional[Dict[str, Any]]:
    meta = _BUILTIN_ENTRIES.get(str(memory_id or ""))
    if not meta:
        return None
    out: Dict[str, Any] = {
        "memory_id": memory_id,
        "title": meta["title"],
        "triggers": list(meta["triggers"]),
        "scope": "global",
        "scope_target": None,
        "status": "active",
        "confidence": 1.0,
        "use_count": 0,
        "last_used_at": None,
        "file_path": "",
        "summary": meta["summary"],
        "source_job_id": None,
        "source_generation": None,
        "source_ai_config_id": None,
        "source_message_id": None,
        "created_at": _BUILTIN_UPDATED_AT,
        "updated_at": _BUILTIN_UPDATED_AT,
    }
    if with_body:
        if user_id and memory_id in ("builtin.intrinsic_personas", "builtin.system_prompts"):
            try:
                kb_store.ensure_user_kb(int(user_id))
            except Exception as exc:
                logger.info(f"ensure_user_kb user={user_id} failed: {exc}")
        if memory_id == "builtin.intrinsic_personas":
            personas = _intrinsic_personas_payload(int(user_id or 0))
            out["intrinsic_personas"] = personas
            out["body"] = _render_intrinsic_personas_body(personas)
        elif memory_id == "builtin.system_prompts":
            prompts = _system_prompts_payload(int(user_id or 0))
            out["system_prompts"] = prompts
            out["body"] = _render_system_prompts_body(prompts)
        elif memory_id == "builtin.intrinsic_properties":
            properties = _intrinsic_properties_payload(int(user_id or 0))
            out["intrinsic_properties"] = properties
            out["body"] = _render_intrinsic_properties_body(properties)
        elif memory_id == "builtin.inheritance_skills":
            skills = _inheritance_skills_payload(int(user_id or 0))
            out["inheritance_skills"] = skills
            out["body"] = _render_inheritance_skills_body(skills)
        elif memory_id == "builtin.inheritance_tools":
            thoughts = _inheritance_thoughts_payload(int(user_id or 0))
            out["inheritance_tools"] = thoughts
            out["body"] = _render_inheritance_thoughts_body(thoughts)
    return out
_INTRINSIC_SCOPE_DESCRIPTIONS = {
    "all": "系统当前固定注册的服务端 MCP 工具定义如下；默认中文展示，编辑后会同步影响 [动态 MCP 说明] 目录与 mcp.describe_tool 的返回。",
    "toolbox": "「工具箱」：每个 AI 默认即可用的系统固定 MCP 工具（无需绑定）；编辑后会同步影响 mcp.describe_tool 的返回。",
    "library": "「图书馆」管理工具：需要该 AI 绑定图书馆后才能调用的治理 / 管理类 MCP（prompt 管理、管理员操作、设备管理、知识库管理）。",
}


def _intrinsic_properties_payload(user_id: int = 0, *, scope: str = "all") -> Dict[str, Any]:
    """服务端固定 MCP 工具视图。``scope``：all 全部 / toolbox 工具箱 / library 图书馆管理工具。"""
    from mcp_runtime.mcp import registry
    from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS

    overrides = _load_intrinsic_properties_overrides(user_id) if user_id else {}
    all_tools = sorted(registry.list_tools(), key=lambda item: str(item.get("name") or ""))
    # 文件为真相源：首次把注册表工具导出成 mcp/<ns>/<tool>.md（已存在跳过）。始终按全量
    # seed，避免按 scope 过滤后漏建文件。
    if user_id:
        try:
            kb_store.seed_mcp_tools(
                int(user_id),
                all_tools,
                lambda nm, schema: _mcp_schema_parameter_rows(nm, schema, None),
            )
        except Exception as exc:
            logger.info(f"seed_mcp_tools user={user_id} failed: {exc}")
    if scope == "toolbox":
        tools = [t for t in all_tools if str(t.get("name") or "").strip() not in LIBRARY_BOUND_TOOLS]
    elif scope == "library":
        tools = [t for t in all_tools if str(t.get("name") or "").strip() in LIBRARY_BOUND_TOOLS]
    else:
        tools = all_tools
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        namespace = name.split(".", 1)[0] if "." in name else "other"
        input_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        override = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
        # 参数描述同样文件优先：mcp md 解析出的 params 覆盖旧 override JSON。
        merged_params = dict(override.get("parameters") or {}) if override else {}
        if user_id:
            merged_params.update(kb_store.effective_param_descriptions(int(user_id), name))
        grouped.setdefault(namespace, []).append({
            "name": name,
            "description": intrinsic_tool_description(user_id, name, str(tool.get("description") or "").strip()),
            "inputSchema": intrinsic_input_schema(user_id, name, input_schema),
            "parameters": _mcp_schema_parameter_rows(
                name,
                input_schema,
                merged_params or None,
            ),
            "destructive": bool(tool.get("destructive")),
            "source": "server",
        })

    categories = [
        {
            "namespace": namespace,
            "count": len(items),
            "tools": items,
        }
        for namespace, items in sorted(grouped.items())
    ]
    return {
        "description": _INTRINSIC_SCOPE_DESCRIPTIONS.get(scope, _INTRINSIC_SCOPE_DESCRIPTIONS["all"]),
        "scope": scope,
        "total": len(tools),
        "categories": categories,
    }


def _mcp_schema_parameter_rows(tool_name: str, schema: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required") if isinstance(schema, dict) else []
    required_set = {str(item) for item in required if str(item).strip()} if isinstance(required, list) else set()
    rows: List[Dict[str, Any]] = []
    for name, config in properties.items():
        cfg = config if isinstance(config, dict) else {}
        raw_type = cfg.get("type", "")
        if isinstance(raw_type, list):
            type_name = " | ".join(str(item) for item in raw_type if str(item).strip())
        else:
            type_name = str(raw_type or "").strip()
        param_name = str(name)
        override_description = ""
        if isinstance(overrides, dict):
            override_description = str(overrides.get(param_name) or "").strip()
        rows.append({
            "name": param_name,
            "type": type_name or "any",
            "required": param_name in required_set,
            "description": override_description or str(cfg.get("description") or "").strip(),
        })
    rows.sort(key=lambda item: (not bool(item.get("required")), str(item.get("name") or "")))
    return rows


def intrinsic_tool_description(user_id: int, name: str, raw: str) -> str:
    # 文件为真相源：mcp/<ns>/<tool>.md 优先；其次旧的 override JSON；最后注册表原文。
    if user_id:
        file_desc = kb_store.effective_tool_description(int(user_id), str(name or "").strip(), "")
        if file_desc:
            return file_desc
    override = _load_intrinsic_properties_overrides(user_id).get(str(name or "").strip()) if user_id else {}
    if isinstance(override, dict):
        description = str(override.get("description") or "").strip()
        if description:
            return description
    return str(raw or "").strip()


def intrinsic_input_schema(user_id: int, tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    properties = out.get("properties")
    if not isinstance(properties, dict):
        return out
    override = _load_intrinsic_properties_overrides(user_id).get(str(tool_name or "").strip()) if user_id else {}
    param_overrides = dict(override.get("parameters") or {}) if isinstance(override, dict) else {}
    # 文件为真相源：mcp md 的参数描述覆盖旧 override JSON。
    if user_id:
        param_overrides.update(kb_store.effective_param_descriptions(int(user_id), str(tool_name or "").strip()))
    for name, config in properties.items():
        if not isinstance(config, dict):
            continue
        param_name = str(name)
        override_description = ""
        if isinstance(param_overrides, dict):
            override_description = str(param_overrides.get(param_name) or "").strip()
        config["description"] = override_description or str(config.get("description") or "").strip()
    return out


def _intrinsic_properties_overrides_path(user_id: int) -> str:
    return os.path.join(_kb_root(user_id), _INTRINSIC_PROPERTIES_OVERRIDES_FILE)


def _load_intrinsic_properties_overrides(user_id: int) -> Dict[str, Any]:
    if not user_id:
        return {}
    path = _intrinsic_properties_overrides_path(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tools = data.get("tools") if isinstance(data, dict) else {}
        return tools if isinstance(tools, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.info(f"{exc}")
        return {}


def save_intrinsic_properties_overrides(*, user_id: int, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    from mcp_runtime.mcp import registry

    current = _load_intrinsic_properties_overrides(user_id)
    schema_by_name = {
        str(t.get("name") or "").strip(): (t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {})
        for t in registry.list_tools()
    }
    destructive_by_name = {
        str(t.get("name") or "").strip(): bool(t.get("destructive"))
        for t in registry.list_tools()
    }
    for item in tools or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        parameters_raw = item.get("parameters")
        parameters: Dict[str, str] = {}
        if isinstance(parameters_raw, list):
            for param in parameters_raw:
                if not isinstance(param, dict):
                    continue
                param_name = str(param.get("name") or "").strip()
                if param_name:
                    parameters[param_name] = str(param.get("description") or "").strip()
        elif isinstance(parameters_raw, dict):
            parameters = {
                str(key).strip(): str(value or "").strip()
                for key, value in parameters_raw.items()
                if str(key).strip()
            }
        description = str(item.get("description") or "").strip()
        current[name] = {
            "description": description,
            "parameters": parameters,
        }
        # 文件为真相源：把本次编辑写入 mcp/<ns>/<tool>.md（权威）。
        try:
            param_rows = _mcp_schema_parameter_rows(name, schema_by_name.get(name) or {}, parameters)
            kb_store.write_mcp_tool(int(user_id), name, description, param_rows, destructive_by_name.get(name, False))
        except Exception as exc:
            logger.info(f"write_mcp_tool {name} failed: {exc}")
    # 旧 override JSON 继续保留作为兼容镜像。
    with open(_intrinsic_properties_overrides_path(user_id), "w", encoding="utf-8") as f:
        json.dump({"tools": current, "updated_at": time.time()}, f, ensure_ascii=False, indent=2)
    return _builtin_entry("builtin.intrinsic_properties", user_id=user_id, with_body=True) or {}


def _render_intrinsic_properties_body(payload: Optional[Dict[str, Any]] = None, *, title: str = "固有属性") -> str:
    data = payload or _intrinsic_properties_payload()
    lines = [
        f"# {title}",
        "",
        str(data.get("description") or ""),
        "",
        f"工具总数：{int(data.get('total') or 0)}",
        "",
    ]
    for category in data.get("categories") or []:
        namespace = str(category.get("namespace") or "")
        lines.append(f"## {namespace}")
        lines.append("")
        for tool in category.get("tools") or []:
            name = str(tool.get("name") or "").strip()
            description = str(tool.get("description") or "").strip() or "（无描述）"
            destructive = "（可能产生写入/变更）" if tool.get("destructive") else ""
            lines.append(f"- `{name}`{destructive}: {description}")
            params = tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
            if params:
                for param in params:
                    required = "必填" if param.get("required") else "可选"
                    param_name = str(param.get("name") or "").strip()
                    param_type = str(param.get("type") or "any").strip()
                    param_desc = str(param.get("description") or "").strip() or "（无描述）"
                    lines.append(f"  - 参数 `{param_name}` ({param_type}, {required}): {param_desc}")
            else:
                lines.append("  - 参数：无")
        lines.append("")
    return "\n".join(lines).strip()


def _render_library_mcp_full_body(payload: Optional[Dict[str, Any]] = None) -> str:
    data = payload if isinstance(payload, dict) else {}

    def _append_categories(lines: List[str], section: Dict[str, Any]) -> None:
        for category in section.get("categories") or []:
            namespace = str(category.get("namespace") or "")
            lines.append(f"### {namespace}")
            lines.append("")
            for tool in category.get("tools") or []:
                name = str(tool.get("name") or "").strip()
                description = str(tool.get("description") or "").strip() or "（无描述）"
                destructive = "（可能产生写入/变更）" if tool.get("destructive") else ""
                lines.append(f"- `{name}`{destructive}: {description}")

    lines = [
        "# 图书馆管理工具",
        "",
        str(data.get("description") or ""),
        "",
        f"工具总数：{int(data.get('total') or 0)}",
        "",
    ]
    governance = data.get("governance") if isinstance(data.get("governance"), dict) else {}
    if governance:
        lines.extend([
            "## 治理类工具（AI 配置 mcp_tools 开关）",
            "",
            str(governance.get("description") or "").strip(),
            "",
        ])
        _append_categories(lines, governance)
        lines.append("")
    return "\n".join(lines).strip()


def _inheritance_skills_payload(user_id: int = 0) -> Dict[str, Any]:
    """传承技能 = 工具箱（默认可用服务端工具） + 图书管理工具（治理类，需图书馆绑定） + 当前账号在线端侧实时上报的工具。

    工具箱 与 图书管理工具 作为独立的“设备”出现在传承技能中，不再是单独的知识库条目。
    """
    toolbox_payload = _intrinsic_properties_payload(int(user_id or 0), scope="toolbox")
    library_payload = _intrinsic_properties_payload(int(user_id or 0), scope="library")
    # server_categories 仍用全量（用于服务端工具描述编辑）
    server_payload = _intrinsic_properties_payload(int(user_id or 0))
    server_categories = server_payload.get("categories") if isinstance(server_payload.get("categories"), list) else []

    def _flatten_tools(payload: Dict[str, Any], device_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for category in (payload or {}).get("categories") or []:
            if not isinstance(category, dict):
                continue
            namespace = str(category.get("namespace") or "").strip()
            for spec in category.get("tools") or []:
                if not isinstance(spec, dict):
                    continue
                name = str(spec.get("name") or "").strip()
                if not name:
                    continue
                out.append({
                    **spec,
                    "namespace": namespace,
                    "device_id": device_id,
                    "device_type": "server",
                    "source": "server",
                })
        return out

    toolbox_tools = _flatten_tools(toolbox_payload, "toolbox")
    library_tools = _flatten_tools(library_payload, "library")

    enriched_devices: List[Dict[str, Any]] = [
        {
            "device_id": "toolbox",
            "device_type": "server",
            "updated_at": _BUILTIN_UPDATED_AT,
            "tool_count": len(toolbox_tools),
            "tools": toolbox_tools,
        },
        {
            "device_id": "library",
            "device_type": "server",
            "updated_at": _BUILTIN_UPDATED_AT,
            "tool_count": len(library_tools),
            "tools": library_tools,
        },
    ]
    tools: List[Dict[str, Any]] = list(toolbox_tools) + list(library_tools)

    online_devices: List[Dict[str, Any]] = []
    try:
        from api.devices.presence import online_tool_catalog_for_user

        online_devices = online_tool_catalog_for_user(int(user_id or 0))
    except Exception as exc:
        logger.info(f"inheritance skills payload failed: {exc}")
        online_devices = []

    for device in online_devices:
        device_id = str(device.get("device_id") or "")
        device_type = str(device.get("device_type") or "desktop")
        device_tools: List[Dict[str, Any]] = []
        for spec in device.get("tools") or []:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            schema = spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {}
            tool = {
                "name": name,
                "description": str(spec.get("description") or "").strip(),
                "inputSchema": schema,
                "parameters": _mcp_schema_parameter_rows(name, schema, None),
                "destructive": bool(spec.get("destructive")),
                "device_id": device_id,
                "device_type": device_type,
                "implementation": spec.get("implementation") if isinstance(spec.get("implementation"), dict) else {},
            }
            device_tools.append(tool)
            tools.append(tool)
        enriched_devices.append({
            "device_id": device_id,
            "device_type": device_type,
            "updated_at": float(device.get("updated_at") or 0),
            "tool_count": len(device_tools),
            "tools": device_tools,
        })
    return {
        "description": (
            "传承技能包含工具箱（默认可用）、图书管理工具（治理类需绑定图书馆）"
            "以及当前账号在线设备实时上报的工具；服务端工具说明可编辑，保存后同步 mcp.list_tools / mcp.describe_tool。"
        ),
        "workshop": "图书馆（内置）",
        "online": bool(online_devices),
        "server_total": len(toolbox_tools) + len(library_tools),
        "server_categories": server_categories,
        "devices": enriched_devices,
        "device_total": len(enriched_devices),
        "endpoint_device_total": len(online_devices),
        "total": len(tools),
        "tools": tools,
    }


def _render_inheritance_skills_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 传承技能",
        "",
        str(payload.get("description") or ""),
        "",
        f"图书馆：{payload.get('workshop') or ''}",
        f"服务端 MCP 工具数：{int(payload.get('server_total') or 0)}",
        f"设备数：{int(payload.get('device_total') or 0)}（端侧 {int(payload.get('endpoint_device_total') or 0)}）",
        f"传承 MCP 工具总数：{int(payload.get('total') or 0)}",
        "",
    ]
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    if not tools:
        lines.append("暂无在线 MCP 工具。")
        return "\n".join(lines).strip()
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "").strip() or "（无描述）"
        source = f"{tool.get('device_type') or 'device'}:{tool.get('device_id') or ''}"
        lines.append(f"- `{name}` [{source}]: {description}")
        params = tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
        if params:
            for param in params:
                required = "必填" if param.get("required") else "可选"
                param_name = str(param.get("name") or "").strip()
                param_type = str(param.get("type") or "any").strip()
                param_desc = str(param.get("description") or "").strip() or "（无描述）"
                lines.append(f"  - 参数 `{param_name}` ({param_type}, {required}): {param_desc}")
        else:
            lines.append("  - 参数：无")
        implementation = tool.get("implementation") if isinstance(tool.get("implementation"), dict) else {}
        if implementation:
            kind = str(implementation.get("kind") or "unknown")
            lines.append(f"  - 实现类型：`{kind}`")
            source_files = implementation.get("source_files") if isinstance(implementation.get("source_files"), list) else []
            if source_files:
                lines.append("  - 源码入口：" + "、".join(f"`{str(item)}`" for item in source_files if str(item).strip()))
            editable_via = str(implementation.get("editable_via") or "").strip()
            if editable_via:
                lines.append(f"  - 修改入口：`{editable_via}`（先 `inspect`，再 `get_source` / `upsert`）")
            code = implementation.get("code")
            if isinstance(code, list):
                lines.extend(["  - 动态实现：", "", "```json", json.dumps(code, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines).strip()


def _intrinsic_personas_payload(user_id: int) -> Dict[str, Any]:
    with Session(engine) as session:
        rows = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user_id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()

    agents: List[Dict[str, Any]] = []
    for cfg in rows:
        agents.append({
            "id": cfg.id,
            "name": cfg.name,
            "description": cfg.description,
            "role": cfg.ai_role,
            "digital_member_role": cfg.digital_member_role,
            "is_librarian": bool(cfg.is_librarian),
            "enabled": bool(cfg.enabled),
            "model": cfg.model,
            "platform": cfg.platform,
            "generation": cfg.generation,
            "prompt": str(kb_store.effective_ai_prompt(cfg.user_id, cfg) or "").strip(),
            "updated_at": cfg.updated_at,
        })

    return {
        "description": "当前用户下所有 AI 的固定人格 prompt 如下。",
        "total": len(agents),
        "agents": agents,
    }


def _render_intrinsic_personas_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 固有人格",
        "",
        str(payload.get("description") or ""),
        "",
        f"AI 总数：{int(payload.get('total') or 0)}",
        "",
    ]
    for agent in payload.get("agents") or []:
        lines.append(f"## {agent.get('name') or agent.get('id')}")
        lines.append("")
        lines.append(f"- ID：{agent.get('id')}")
        lines.append(f"- 角色：{agent.get('role') or ''}")
        lines.append(f"- 模型：{agent.get('model') or ''}")
        lines.append("")
        lines.append("### 人格 Prompt")
        lines.append("")
        lines.append(str(agent.get("prompt") or "（空）"))
        lines.append("")
    return "\n".join(lines).strip()


def save_intrinsic_persona(
    *,
    user_id: int,
    ai_config_id: int,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """更新一个 AI 的固有人格 Prompt。

    只更新显式给出的字段；文件为真相源，写入 personas/*.md。"""
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.id == int(ai_config_id),
                AssistantAIConfig.user_id == int(user_id),
            )
        ).first()
        if not cfg:
            raise ValueError("AI config not found")

        effective_prompt = (
            str(prompt) if prompt is not None
            else kb_store.effective_ai_prompt(int(user_id), cfg)
        )
        kb_store.write_persona(int(user_id), cfg, prompt=effective_prompt)
        cfg.updated_at = time.time()
        session.add(cfg)
        session.commit()
        session.refresh(cfg)

    return _builtin_entry("builtin.intrinsic_personas", user_id=user_id, with_body=True) or {}


_SYSTEM_PROMPT_SECTIONS = [
    {
        "key": "mcp",
        "title": "MCP 提示词",
        "items": [
            ("mcp_call_method", "全局 MCP 调用规范", "text"),
            ("mcp_namespace_hints", "MCP namespace 说明（JSON）", "text"),
            ("mcp_dynamic_rule", "MCP 动态工具暴露规则", "text"),
            ("mcp_format_error_hint", "MCP 格式错误提示", "text"),
        ],
    },
    {
        "key": "task",
        "title": "统一任务提示词",
        "items": [
            ("default_start_task_prompt", "任务启动 Prompt", "text"),
            ("default_resume_task_prompt", "任务恢复 Prompt", "text"),
            ("default_supervision_prompt", "监督 Prompt", "text"),
            ("default_supervision_idle_seconds", "AI 停止思考提醒秒数", "number"),
            ("default_compression_prompt", "对话压缩 Prompt", "text"),
            ("task_plan_flow_prompt", "任务分阶段流程 Prompt", "text"),
        ],
    },
    {
        "key": "communication",
        "title": "AI 通信提示词",
        "items": [
            ("prompt_ai_message_notify", "AI 间消息·通知模板", "text"),
            ("prompt_ai_message_inquiry", "AI 间消息·询问模板", "text"),
            ("ai_message_inquiry_reminder_seconds", "询问未回复提醒秒数", "number"),
            ("prompt_ai_message_inquiry_reminder", "AI 间询问未回复提醒模板", "text"),
            ("prompt_ai_message_reply", "AI 间消息·回复模板", "text"),
            ("prompt_ai_message_chitchat", "AI 间消息·闲聊模板", "text"),
            ("prompt_ai_message_reply_success", "AI 间消息回复成功提示", "text"),
            ("prompt_user_message_notice", "用户消息发送提示", "text"),
        ],
    },
]


def _system_prompts_payload(user_id: int) -> Dict[str, Any]:
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            return {"description": "系统设置中的提示词配置。", "total": 0, "sections": []}
        sections: List[Dict[str, Any]] = []
        total = 0
        for section in _SYSTEM_PROMPT_SECTIONS:
            items: List[Dict[str, Any]] = []
            for field, label, value_type in section["items"]:
                # 文本提示词真相源在 system/*.md；数值设置仍读数据库列。
                if value_type == "number":
                    value = getattr(user, field, "")
                else:
                    value = kb_store.effective_system_value(user_id, field, getattr(user, field, None))
                items.append({
                    "key": field,
                    "label": label,
                    "type": value_type,
                    "content": str(value if value is not None else ""),
                })
            total += len(items)
            sections.append({
                "key": section["key"],
                "title": section["title"],
                "count": len(items),
                "items": items,
            })
        return {
            "description": "系统统一提示词如下；任务启动、恢复、监督与传承 Prompt 保存后对所有 AI 生效。",
            "total": total,
            "sections": sections,
        }


def save_system_prompts(*, user_id: int, prompts: List[Dict[str, Any]]) -> Dict[str, Any]:
    allowed: Dict[str, str] = {
        field: value_type
        for section in _SYSTEM_PROMPT_SECTIONS
        for field, _label, value_type in section["items"]
    }
    # 记录文本字段的最终值，提交后据此写文件（不依赖已删列的 getattr）。
    applied_text: Dict[str, str] = {}
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("user not found")
        for item in prompts or []:
            key = str(item.get("key") or "").strip()
            if key not in allowed:
                continue
            raw = item.get("content")
            if allowed[key] == "number":
                # 数值设置项仍存数据库列。
                try:
                    value = int(raw if raw not in {None, ""} else 0)
                except Exception:
                    value = 0
                if key == "default_supervision_idle_seconds":
                    value = max(5, min(3600, value or 25))
                elif key == "ai_message_inquiry_reminder_seconds":
                    value = max(0, min(3600, value))
                setattr(user, key, value)
            elif key == "mcp_namespace_hints":
                raw_text = str(raw or "").strip()
                if raw_text:
                    try:
                        parsed = json.loads(raw_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("mcp_namespace_hints must be a JSON object")
                        raw_text = json.dumps(
                            {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip() and str(v).strip()},
                            ensure_ascii=False,
                        )
                    except Exception:
                        raise ValueError("mcp_namespace_hints must be a JSON object")
                applied_text[key] = raw_text
            elif key == "mcp_call_method":
                text = "\n".join(
                    line for line in str(raw or "").splitlines()
                    if "Call exactly one tool per <mcp-call> block; never join two tool names into one name." not in line
                ).strip()
                applied_text[key] = text
            else:
                applied_text[key] = str(raw or "")
        session.add(user)
        session.commit()
    # 文件为真相源：文本提示词写入 system/<key>.md（权威）。
    for key, value in applied_text.items():
        kb_store.write_system_prompt(int(user_id), key, value)
        if kb_store.read_system_prompt(int(user_id), key) != value.strip():
            raise RuntimeError(f"failed to persist system prompt: {key}")
    return _builtin_entry("builtin.system_prompts", user_id=user_id, with_body=True) or {}


def _render_system_prompts_body(payload: Dict[str, Any]) -> str:
    lines = [
        "# 固有思想",
        "",
        str(payload.get("description") or ""),
        "",
        f"配置项总数：{int(payload.get('total') or 0)}",
        "",
    ]
    for section in payload.get("sections") or []:
        lines.append(f"## {section.get('title') or section.get('key')}")
        lines.append("")
        for item in section.get("items") or []:
            lines.append(f"### {item.get('label') or item.get('key')}")
            lines.append("")
            lines.append(str(item.get("content") or "（空）"))
            lines.append("")
    return "\n".join(lines).strip()


