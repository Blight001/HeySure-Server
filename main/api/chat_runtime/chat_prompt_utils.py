"""Prompt-assembly utilities for chat runs: strip/inject runtime prompt sections,
render the MCP tool catalog and namespace hints into system prompts, and maintain
per-run live text/reasoning/usage state for streaming."""

IS_ROUTER_ENTRY = False

import asyncio
import json
import logging
import re
import time


logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from mcp_runtime.mcp import registry
from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
from api.models import AssistantAIConfig, DEFAULT_MCP_FORMAT_ERROR_HINT
from api.models.defaults import DEFAULT_MCP_NAMESPACE_HINTS
from api.chat_runtime.mcp_parser import (
    MCP_CALL_BLOCK_RE,
    extract_first_complete_mcp_call,
    parse_mcp_payload,
)
from connector_runtime.dispatch.desktop_device_tools import (
    endpoint_bridge_tools_for_config,
    endpoint_tools_for_config,
)
from api.services.tasks.task_system import (
    with_workspace_read_by_name_compat,
)
from .run_state import (
    STATE_PREFIX,
    _AUTO_RUNTIME_SECTION_TITLES,
    _RUN_LIVE_STATE,
    _RUN_LIVE_META,
    _RUN_STATE_LOCK,
    _TASK_CREATE_TOOL_NAMES,
    _TASK_RUNTIME_SECTION_TITLES,
    get_run_stream,
)
from api.sio import sio

def _parse_mcp_payload(raw: str):
    return parse_mcp_payload(raw)

def _strip_prompt_section(text: str, section_title: str) -> str:
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    pattern = re.compile(rf"\n*\[{re.escape(section_title)}\]\n[\s\S]*?(?=\n\[[^\n]+\]\n|$)")
    return pattern.sub("", src)

def _strip_prompt_sections(text: str, section_titles: tuple[str, ...]) -> str:
    current = str(text or "")
    for section_title in section_titles:
        current = _strip_prompt_section(current, section_title)
    return current.strip()

def _append_prompt_section(text: str, section_title: str, section_body: str) -> str:
    base = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    body = str(section_body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return base
    section = f"[{section_title}]\n{body}"
    if not base:
        return section
    return f"{base}\n\n{section}"

def _strip_runtime_injected_sections(text: str) -> str:
    return _strip_prompt_sections(text, _AUTO_RUNTIME_SECTION_TITLES)

def _strip_task_runtime_sections(text: str) -> str:
    return _strip_prompt_sections(text, _TASK_RUNTIME_SECTION_TITLES)

def _strip_legacy_global_mcp_block(text: str) -> str:
    # Remove previously inlined global MCP method blocks to avoid duplicate prompt sections.
    return _strip_prompt_section(text, "全局MCP调用方法").strip()

def _looks_like_mcp_template(text: str) -> bool:
    src = str(text or "").strip().lower()
    if not src:
        return False
    has_tools_line = (
        "available mcp tools include" in src
        or "可用的 mcp 工具包括" in src
        or "可用的mcp工具包括" in src
    )
    has_rules_line = ("rules:" in src or "规则" in src)
    return ("<mcp-call>" in src and has_tools_line and has_rules_line)

def _parse_allowed_tools_for_cfg(cfg: Optional[AssistantAIConfig]) -> set[str]:
    from connector_runtime.dispatch.desktop_device_tools import strip_endpoint_tool_config_names

    if not cfg:
        return set()
    try:
        parsed = json.loads(cfg.mcp_tools or "[]")
        if not isinstance(parsed, list):
            return set()
        raw_tools = {str(item).strip() for item in parsed if isinstance(item, str) and str(item).strip()}
        from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names
        raw_tools = fully_clean_tool_names(raw_tools)
        raw_tools = strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(raw_tools))
        raw_tools.update(MCP_INTROSPECTION_TOOLS)
        raw_tools.update(endpoint_bridge_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
        raw_tools.update(endpoint_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
        return raw_tools
    except Exception:
        return set()

def _schema_type_name(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    raw = schema.get("type")
    if isinstance(raw, list):
        vals = [str(v).strip() for v in raw if str(v).strip()]
        return "|".join(vals) if vals else "any"
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(schema.get("enum"), list):
        return "enum"
    return "any"

def _example_schedule_iso(hours_from_now: int = 2) -> str:
    target = datetime.now().astimezone() + timedelta(hours=max(1, int(hours_from_now or 1)))
    return target.replace(second=0, microsecond=0).isoformat()

def _schema_placeholder(schema: Any, field_name: str = "") -> Any:
    t = _schema_type_name(schema)
    field = str(field_name or "").strip().lower()
    if "|" in t:
        candidates = [part.strip() for part in t.split("|") if part.strip()]
        if "string" in candidates:
            t = "string"
        elif "number" in candidates:
            t = "number"
        elif "integer" in candidates:
            t = "integer"
        elif candidates:
            t = candidates[0]

    if t == "string":
        if field in {"path", "name"}:
            return "README.md"
        if field == "command":
            return "git status"
        if field in {"job_id"}:
            return "job_demo_001"
        if field in {"project_id", "id"}:
            return "project_demo_001"
        if field == "title":
            return "整理今日开发待办"
        if field in {"instruction", "content"}:
            return "先检查当前进度，再输出下一步执行清单。"
        return "example"
    if t == "integer":
        if field in {"priority"}:
            return 5
        if field in {"target_ai_config_id", "target_config_id"}:
            return 1
        return 1
    if t == "number":
        return 1
    if t == "boolean":
        return True
    if t == "array":
        if field in {"paths", "names"}:
            return ["README.md"]
        if field in {"ai_member_ids"}:
            return [1, 2]
        return ["example"]
    if t == "object":
        if field == "flowdata":
            return {"action": "ping"}
        return {"note": "example"}
    if t == "enum":
        values = schema.get("enum") if isinstance(schema, dict) else None
        if isinstance(values, list) and values:
            return values[0]
        return "example"
    return "example"

def _compact_fields(fields: List[str], limit: int = 6) -> str:
    if not fields:
        return "无"
    if len(fields) <= limit:
        return ", ".join(fields)
    return ", ".join(fields[:limit]) + ", ..."

def _render_mcp_tool_item(tool_info: Dict[str, Any]) -> str:
    name = str(tool_info.get("name") or "").strip()
    if not name:
        return ""
    schema = tool_info.get("inputSchema")
    if not isinstance(schema, dict):
        return f"- {name} (参数: 无)"

    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return f"- {name} (参数: 无)"

    required_raw = schema.get("required")
    required_names: List[str] = []
    if isinstance(required_raw, list):
        required_names = [str(v).strip() for v in required_raw if str(v).strip() in props]
    optional_names = [k for k in props.keys() if k not in required_names]

    required_desc = [f"{key}:{_schema_type_name(props.get(key))}" for key in required_names]
    optional_desc = [f"{key}:{_schema_type_name(props.get(key))}" for key in optional_names]
    field_limit = 100 if name in _TASK_CREATE_TOOL_NAMES else (30 if name.startswith("task.") else 6)

    example_args: Dict[str, Any] = {}
    if name == "task.manage":
        example_args["action"] = "create"
        example_args["mode"] = "scheduled"
        if "title" in props:
            example_args["title"] = "两小时后执行代码健康巡检"
        if "instruction" in props:
            example_args["instruction"] = "运行后端语法检查与前端类型检查，记录失败项和修复建议到 doc/ops/health-check.md。"
        if "priority" in props:
            example_args["priority"] = 6
        if "schedule_at" in props:
            example_args["schedule_at"] = _example_schedule_iso(2)
    else:
        seed_names = required_names[:3] if required_names else optional_names[:1]
        for key in seed_names:
            example_args[key] = _schema_placeholder(props.get(key), key)

    example_payload = json.dumps(
        {"tool": name, "arguments": example_args},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    format_hint = ""
    if name == "task.manage":
        format_hint = (
            "\n"
            "  action: `list`/`create`/`update`/`delete`；create/update/delete 需管理者及以上。\n"
            "  mode: `immediate` 立即执行；`scheduled` 一次性定时；`recurring` 循环运行。\n"
            "  时间格式: mode=scheduled 的 `schedule_at` 仅支持 Unix 秒或带时区 ISO-8601（必须包含 `+08:00` 或 `Z`）；"
            "mode=recurring 不传 `schedule_at`，仅使用 `schedule_duration_minutes`（分钟间隔）。"
        )
    return (
        f"- {name} (必填: {_compact_fields(required_desc, field_limit)}; 可选: {_compact_fields(optional_desc, field_limit)})\n"
        f"  示例: {example_payload}"
        f"{format_hint}"
    )

def _tool_namespace(name: str) -> str:
    if "." in name:
        return name.split(".", 1)[0]
    if "_" in name:
        return name.split("_", 1)[0]
    return "other"

def _render_mcp_tool_lines(cfg: Optional[AssistantAIConfig]) -> str:
    if cfg is not None:
        if not getattr(cfg, "mcp_enabled", True):
            return "- （MCP 未启用）"
    return _render_mcp_namespace_lines(cfg, DEFAULT_MCP_NAMESPACE_HINTS)

def _parse_namespace_hints(raw: str) -> Dict[str, str]:
    try:
        parsed = json.loads(str(raw or "").strip() or DEFAULT_MCP_NAMESPACE_HINTS)
    except Exception:
        parsed = json.loads(DEFAULT_MCP_NAMESPACE_HINTS)
    if not isinstance(parsed, dict):
        parsed = json.loads(DEFAULT_MCP_NAMESPACE_HINTS)
    fallback = json.loads(DEFAULT_MCP_NAMESPACE_HINTS)
    out: Dict[str, str] = {str(k): str(v) for k, v in fallback.items()}
    for key, value in parsed.items():
        namespace = str(key or "").strip()
        hint = str(value or "").strip()
        if namespace and hint:
            out[namespace] = hint
    return out

def _render_mcp_namespace_lines(cfg: Optional[AssistantAIConfig], namespace_hints: str) -> str:
    # ``namespace_hints`` kept for signature/back-compat. Parent namespace names
    # (browser / workspace / …) are intentionally no longer enumerated here: a
    # first-name segment is not itself a callable MCP, and listing it invited the
    # model to describe/query the bare parent (dumping every tool under it). The
    # concrete per-turn tool list is supplied via the "[本轮可用 MCP 工具]" catalog,
    # so {MCP} only renders unified usage guidance now.
    if cfg is not None and not getattr(cfg, "mcp_enabled", True):
        return "- （MCP 未启用）"
    allowed = _parse_allowed_tools_for_cfg(cfg)
    if cfg is None:
        allowed = {str(item.get("name") or "").strip() for item in registry.list_tools() if item.get("name")}
    if not allowed:
        return "- （空）"
    return (
        "- 本轮可用的具体 MCP 工具清单见对话中的「[本轮可用 MCP 工具]」；"
        "需要某个工具的参数 schema 时用 mcp.describe_tool 传入具体工具名（如 workspace.search），"
        "或用具体关键词搜索——不要传 browser/workspace 等父级 namespace 名，父级名不返回内容。"
    )

def _short_tool_desc(text: str, limit: int = 90) -> str:
    """Compress a tool description to a single short line for the catalog."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    # Prefer the first sentence boundary so the catalog line reads cleanly.
    for sep in ("。", "！", "？", ". ", "! ", "? "):
        idx = raw.find(sep)
        if 0 < idx <= limit:
            keep = idx + (1 if sep in "。！？" else 0)
            return raw[:keep].strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip() + "…"


def _render_mcp_tool_catalog(allowed_tools: set[str], endpoint_tools: Optional[set[str]] = None, user_id: int = 0) -> str:
    """Flat one-shot catalog of every allowed tool as ``name: short desc``.

    Mirrors how mature agent runtimes list all tool names up front: the model
    can locate the right tool directly, then load the precise schema in a single
    ``mcp.describe_tool`` call. ``!`` marks destructive tools.

    ``endpoint_tools`` is the set of dynamically-allocated desktop/browser agent
    tools the caller already resolved as currently available for this AI. They
    are authoritative and always listed (with the agent's self-reported
    description), so the catalog never drops a live agent tool just because the
    presence-derived ``is_endpoint_agent_tool`` snapshot disagrees. Any remaining
    non-builtin, non-endpoint name is a stale config entry and is omitted.
    """
    from api.devices.presence import online_tool_defs
    from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool

    allowed = {str(name).strip() for name in (allowed_tools or set()) if str(name).strip()}
    if not allowed:
        return "- （空）"
    endpoint_allowed = {str(name).strip() for name in (endpoint_tools or set()) if str(name).strip()}

    # 文件为真相源：KnowledgeBase/mcp/*.md 的描述优先于注册表原文。
    effective_desc = None
    if user_id:
        try:
            from api.services.knowledge import kb_store

            effective_desc = kb_store.effective_tool_description
        except Exception:
            effective_desc = None
    desc_by_name: Dict[str, str] = {}
    destructive_by_name: Dict[str, bool] = {}
    for item in registry.list_tools():
        name = str(item.get("name") or "").strip()
        if name:
            raw_desc = str(item.get("description") or "").strip()
            if effective_desc is not None:
                try:
                    raw_desc = effective_desc(int(user_id), name, raw_desc)
                except Exception:
                    pass
            desc_by_name[name] = raw_desc
            destructive_by_name[name] = bool(item.get("destructive"))
    endpoint_defs = online_tool_defs()

    groups: Dict[str, List[str]] = {}
    for name in sorted(allowed):
        if name in desc_by_name:
            # Registered builtin tool.
            desc = desc_by_name.get(name, "")
            destructive = destructive_by_name.get(name, False)
        elif name in endpoint_allowed or is_endpoint_agent_tool(name):
            # Dynamically-allocated desktop/browser agent tool. Prefer the
            # caller-resolved allow-list; description comes from the agent.
            spec = endpoint_defs.get(name) or {}
            desc = str(spec.get("description") or "").strip()
            destructive = True
        else:
            # Stale name persisted in the config allow-list that no longer maps
            # to any real tool — omit it so the model never sees a dead tool.
            continue
        marker = " !" if destructive else ""
        short = _short_tool_desc(desc)
        line = f"  - {name}{marker}: {short}" if short else f"  - {name}{marker}"
        groups.setdefault(_tool_namespace(name), []).append(line)

    lines: List[str] = []
    for namespace in sorted(groups):
        lines.append(f"{namespace}/")
        lines.extend(groups[namespace])
    return "\n".join(lines) if lines else "- （空）"


def _filter_tools_for_current_bindings(
    allowed: set[str], user_id: int, ai_config_id: Optional[int]
) -> set[str]:
    """Filter out tools that the AI cannot actually use because of missing bindings.

    - LIBRARY_BOUND_TOOLS require library (workshop) binding.
    - System built-in server MCPs (knowledge.search, workspace.*, plan.*, task.*,
      conversation.*, mode.*, etc.) are DIRECT (no toolbox binding required).
      Device/endpoint MCPs remain governed by per-agent scopes.
    - Always preserve introspection tools (mcp.describe_tool etc.) so the model
      can still discover tools even before binding.
    This keeps the catalog honest: only show what can actually be called.
    """
    if not ai_config_id:
        return set(allowed)
    result = set(allowed)
    try:
        from api.devices.workshop_bindings import config_bound_to_library
        from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS
        from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS
        # Note: toolbox binding no longer gates system built-in MCPs (knowledge.search,
        # workspace.*, plan.* etc. are direct). Only library governance remains gated.
        # is_toolbox_gated_tool / config_bound_to_toolbox kept for UI grouping only.

        protected = set(MCP_INTROSPECTION_TOOLS or set())
        if not config_bound_to_library(int(user_id), int(ai_config_id)):
            result -= (set(LIBRARY_BOUND_TOOLS) - protected)

        # Toolbox-gated removal intentionally removed: system server MCPs are now
        # always direct-callable (subject only to mcp_enabled + mode + library for gov tools).
    except Exception:
        # Fail open on any lookup error to avoid breaking catalogs.
        pass
    return result


def _build_dynamic_mcp_explanation(
    allowed: set[str],
    endpoint_allowed: set[str],
    user_id: int,
    ai_config_id: Optional[int],
) -> str:
    """Build the dynamic MCP 说明 in grouped format (工具箱 MCP / 图书馆 MCP etc.)
    using the shared group logic. This is the updated structure for the AI prompt.
    Falls back to flat if anything fails.
    """
    try:
        from api.services.mcp.mcp_prompt_groups import build_prompt_tool_groups
        from mcp_runtime.mcp import registry as mcp_registry
        from api.devices.presence import online_tool_defs
        from connector_runtime.dispatch.desktop_device_tools import is_endpoint_agent_tool
        from api.services.mcp.mcp_tool_aliases import fully_clean_tool_names

        prompt_tools: list[dict] = []
        allowed_set = fully_clean_tool_names(allowed or set())

        # server tools
        for item in mcp_registry.list_tools():
            name = str(item.get("name") or "").strip()
            if name and name in allowed_set:
                prompt_tools.append({
                    "name": name,
                    "description": str(item.get("description") or "").strip(),
                    "destructive": bool(item.get("destructive")),
                    "inputSchema": item.get("inputSchema") or {},
                    "mcpSource": "server",
                })

        # endpoint / dynamic tools that are in the allow list
        try:
            endpoint_defs = online_tool_defs()
        except Exception:
            endpoint_defs = {}
        for name in sorted(allowed_set):
            if name in (endpoint_allowed or set()) or is_endpoint_agent_tool(name):
                spec = endpoint_defs.get(name) or {}
                if not any(p.get("name") == name for p in prompt_tools):
                    prompt_tools.append({
                        "name": name,
                        "description": str(spec.get("description") or "").strip(),
                        "destructive": True,
                        "inputSchema": spec.get("input_schema") or {},
                        "mcpSource": str(spec.get("mcpSource") or "desktop"),
                    })

        groups = build_prompt_tool_groups(
            user_id=int(user_id or 0),
            ai_config_id=ai_config_id,
            prompt_tools=prompt_tools,
            allowed_tools=allowed_set,
        )

        sections: list[str] = []
        for g in groups or []:
            label = str(g.get("groupLabel") or g.get("groupKey") or "").strip()
            tools = g.get("tools") or []
            if not label:
                continue
            if not tools:
                sections.append(f"{label}\n- （当前无可用工具）")
                continue
            # render inner using existing logic on names; pass the authoritative
            # endpoint set so live agent tools the presence snapshot doesn't yet
            # recognise are not dropped from a device group.
            tool_names = {str(t.get("name") or "") for t in tools if str(t.get("name") or "").strip()}
            inner = _render_mcp_tool_catalog(tool_names, endpoint_allowed or set(), user_id)
            sections.append(f"{label}\n{inner}")

        if not sections:
            return "- （空）"
        return "\n\n".join(sections)
    except Exception:
        # fallback to previous flat rendering
        return _render_mcp_tool_catalog(allowed or set(), endpoint_allowed or set(), user_id)


def _inject_mcp_placeholder(template: str, cfg: Optional[AssistantAIConfig]) -> str:
    return _inject_mcp_placeholder_with_hints(template, cfg, DEFAULT_MCP_NAMESPACE_HINTS)

def _inject_mcp_placeholder_with_hints(template: str, cfg: Optional[AssistantAIConfig], namespace_hints: str) -> str:
    text = str(template or "")
    if "{MCP}" not in text:
        return text
    return text.replace("{MCP}", _render_mcp_namespace_lines(cfg, namespace_hints))

def _merge_global_mcp_method(
    system_prompt: str,
    global_mcp_method: str,
    cfg: Optional[AssistantAIConfig],
    namespace_hints: str = DEFAULT_MCP_NAMESPACE_HINTS,
) -> str:
    base = _strip_legacy_global_mcp_block(system_prompt)
    method = _inject_mcp_placeholder_with_hints(str(global_mcp_method or "").strip(), cfg, namespace_hints)
    method = str(method or "").replace("\r\n", "\n").replace("\r", "\n")
    if not method:
        return base
    method = "\n".join(
        line for line in method.splitlines()
        if "Call exactly one tool per <mcp-call> block; never join two tool names into one name." not in line
    ).strip()
    # Legacy bug compatibility: global MCP template was once persisted to admin_prompt directly.
    if _looks_like_mcp_template(base) and method:
        base = ""
    if method in base:
        return base
    if base:
        return f"{base}\n\n[全局MCP调用方法]\n{method}"
    return f"[全局MCP调用方法]\n{method}"

def _render_mcp_warning_text(template: str, details: List[str], values: Dict[str, Any]) -> str:
    details_bullets = "\n".join(f"- {line}" for line in details if str(line).strip())
    if not details_bullets:
        details_bullets = "- 未提供细节"

    payload = dict(values)
    payload["details"] = details_bullets
    payload["details_bullets"] = details_bullets

    def _replace_known_tokens(raw: str) -> str:
        text = str(raw or "")
        for key, value in payload.items():
            text = text.replace("{" + str(key) + "}", str(value))
        return text.strip()

    rendered = _replace_known_tokens(str(template or "").strip() or DEFAULT_MCP_FORMAT_ERROR_HINT)
    if rendered:
        return rendered

    return (
        "[系统提示] 检测到 MCP 调用未成功。\n"
        f"{details_bullets}\n"
        "请使用标准格式: <mcp-call>{\"tool\":\"...\",\"arguments\":{...}}</mcp-call>"
    )

def _build_mcp_stream_warning(
    assistant_text: str,
    cfg: Optional[AssistantAIConfig],
    warning_template: str = "",
) -> Optional[str]:
    matches = list(MCP_CALL_BLOCK_RE.finditer(assistant_text or ""))
    if not matches:
        return None

    parsed_calls = []
    format_error_count = 0
    xml_arguments_tag_count = 0
    for m in matches:
        raw_payload = str(m.group(1) or "")
        payload = _parse_mcp_payload(raw_payload)
        if payload:
            parsed_calls.append(payload)
        else:
            format_error_count += 1
            args_match = re.search(r"<arguments>\s*([\s\S]*?)\s*</arguments>", raw_payload, re.IGNORECASE)
            args_raw = str(args_match.group(1) or "").strip() if args_match else ""
            if args_raw and re.search(r"<\s*[a-zA-Z_][\w.\-]*\b[^>]*>", args_raw):
                xml_arguments_tag_count += 1

    known_tools = {item.get("name") for item in registry.list_tools() if item.get("name")}
    if cfg:
        known_tools.update(
            endpoint_bridge_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None))
        )
        known_tools.update(
            endpoint_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None))
        )

    unauthorized_tools = []
    unknown_tools = []
    if cfg and parsed_calls:
        allowed_tools = set()
        try:
            parsed_allowed = json.loads(cfg.mcp_tools or "[]")
            if isinstance(parsed_allowed, list):
                allowed_tools = {str(v).strip() for v in parsed_allowed if isinstance(v, str) and str(v).strip()}
                from connector_runtime.dispatch.desktop_device_tools import strip_endpoint_tool_config_names

                allowed_tools = strip_endpoint_tool_config_names(with_workspace_read_by_name_compat(allowed_tools))
        except Exception:
            allowed_tools = set()

        if not cfg.mcp_enabled:
            unauthorized_tools = [call["tool"] for call in parsed_calls]
        else:
            allowed_tools.update(endpoint_bridge_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
            allowed_tools.update(endpoint_tools_for_config(getattr(cfg, "id", None), getattr(cfg, "user_id", None)))
            for call in parsed_calls:
                tool = call["tool"]
                if tool not in known_tools:
                    unknown_tools.append(tool)
                elif tool not in allowed_tools:
                    unauthorized_tools.append(tool)
    else:
        for call in parsed_calls:
            tool = call["tool"]
            if tool not in known_tools:
                unknown_tools.append(tool)

    if format_error_count == 0 and not unauthorized_tools and not unknown_tools:
        return None

    hints = []
    if format_error_count > 0:
        hints.append(f"检测到 {format_error_count} 个 mcp-call 块格式错误。")
    if xml_arguments_tag_count > 0:
        hints.append(
            f"其中 {xml_arguments_tag_count} 个调用在 <arguments> 内使用了 XML 子标签；"
            "该位置必须是 JSON 对象字符串。"
        )
    if unknown_tools:
        hints.append(f"未注册工具: {', '.join(sorted(set(unknown_tools)))}")
    if unauthorized_tools:
        hints.append(f"无权限工具: {', '.join(sorted(set(unauthorized_tools)))}")
    values = {
        "format_error_count": format_error_count,
        "xml_arguments_tag_count": xml_arguments_tag_count,
        "unknown_tools": ", ".join(sorted(set(unknown_tools))),
        "unauthorized_tools": ", ".join(sorted(set(unauthorized_tools))),
        "mcp_call_count": len(matches),
    }
    return _render_mcp_warning_text(warning_template, hints, values)

def _stable_stringify(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_stable_stringify(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return "{" + ",".join(f"{json.dumps(str(k), ensure_ascii=False)}:{_stable_stringify(value[k])}" for k in keys) + "}"
    return json.dumps(str(value), ensure_ascii=False)

def _simple_hash(input_text: str) -> str:
    h = 5381
    for ch in input_text:
        h = ((h << 5) + h) + ord(ch)
        h &= 0xFFFFFFFF
    return str(h)

def _block_signature(tool: str, arguments: dict) -> str:
    raw = "|".join([
        "mcp",
        tool or "",
        "",
        "",
        "",
        "",
        "",
        _stable_stringify(arguments or {}),
    ])
    return f"sig_{_simple_hash(raw)}"

def _sanitize_large_media(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "dataUrl" and isinstance(item, str) and item.startswith("data:image/"):
                out[key] = f"<image data URL omitted, {len(item)} chars>"
            else:
                out[key] = _sanitize_large_media(item)
        return out
    if isinstance(value, list):
        return [_sanitize_large_media(item) for item in value]
    return value

def _safe_json(value, max_len: int = 12000) -> str:
    try:
        sanitized = _sanitize_large_media(value)
        text = json.dumps(sanitized, ensure_ascii=False, indent=2)
        if len(text) > max_len:
            # Pretty-printing bloats large payloads 2-3x with whitespace; retry
            # compact so element-heavy tool results (e.g. browser_observe on a
            # busy page) keep their full item list instead of being cut mid-way.
            text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n...<truncated>"

def _extract_mcp_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = getattr(exc, "detail", "")
        if isinstance(detail, (dict, list)):
            return _safe_json(detail, 2000)
        detail_text = str(detail or "").strip()
        if detail_text:
            return detail_text
        return f"HTTP {getattr(exc, 'status_code', 500)}"
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("detail"):
                return str(payload["detail"])
        except Exception:
            response_text = str(getattr(response, "text", "") or "").strip()
            if response_text:
                return response_text
    text = str(exc or "").strip()
    return text or exc.__class__.__name__

def _build_mcp_display_result(tool: str, data: dict, ok: bool = True, error_message: str = "") -> str:
    result = data.get("result", data)
    if ok:
        return _safe_json(result)
    return f"错误: {error_message or '未知错误'}\n\n{_safe_json(result)}"

def _extract_first_mcp_call(assistant_text: str):
    payload, _ = _extract_first_complete_mcp_call(assistant_text)
    return payload

def _extract_first_complete_mcp_call(assistant_text: str):
    return extract_first_complete_mcp_call(assistant_text)

def _extract_delta_text(delta) -> str:
    if not delta:
        return ""
    content = delta.get("content") if isinstance(delta, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""

def _set_run_live_text(run_id: str, text: str):
    with _RUN_STATE_LOCK:
        prev = _RUN_LIVE_STATE.get(run_id) or {}
        meta = _RUN_LIVE_META.get(run_id) or {}
        _RUN_LIVE_STATE[run_id] = {
            "text": text,
            "reasoning": prev.get("reasoning", ""),
            "phase": prev.get("phase", "generating"),
            "current_tool": prev.get("current_tool", ""),
            "pending_prompt_tokens": int(prev.get("pending_prompt_tokens") or 0),
            "pending_completion_tokens": int(prev.get("pending_completion_tokens") or 0),
            "pending_total_tokens": int(prev.get("pending_total_tokens") or 0),
            "updated_at": time.time(),
        }
        _RUN_LIVE_META[run_id] = meta
    _emit_run_live_update(run_id)
    # Mirror the growing answer to any external streaming observer (e.g. a QQ
    # streaming message). Best-effort: the hook self-throttles and never raises.
    hook = get_run_stream(run_id)
    if hook is not None:
        try:
            hook.update(text)
        except Exception:
            pass

def _set_run_live_reasoning(run_id: str, reasoning: str):
    with _RUN_STATE_LOCK:
        prev = _RUN_LIVE_STATE.get(run_id) or {}
        meta = _RUN_LIVE_META.get(run_id) or {}
        _RUN_LIVE_STATE[run_id] = {
            "text": prev.get("text", ""),
            "reasoning": reasoning,
            "phase": prev.get("phase", "generating"),
            "current_tool": prev.get("current_tool", ""),
            "pending_prompt_tokens": int(prev.get("pending_prompt_tokens") or 0),
            "pending_completion_tokens": int(prev.get("pending_completion_tokens") or 0),
            "pending_total_tokens": int(prev.get("pending_total_tokens") or 0),
            "updated_at": time.time(),
        }
        _RUN_LIVE_META[run_id] = meta
    _emit_run_live_update(run_id)


def _get_run_live_reasoning(run_id: str) -> str:
    with _RUN_STATE_LOCK:
        live = _RUN_LIVE_STATE.get(run_id) or {}
        return str(live.get("reasoning") or "")

def _set_run_live_phase(run_id: str, phase: str, current_tool: str = ""):
    with _RUN_STATE_LOCK:
        prev = _RUN_LIVE_STATE.get(run_id) or {}
        meta = _RUN_LIVE_META.get(run_id) or {}
        _RUN_LIVE_STATE[run_id] = {
            "text": prev.get("text", ""),
            "reasoning": prev.get("reasoning", ""),
            "phase": phase,
            "current_tool": current_tool,
            "pending_prompt_tokens": int(prev.get("pending_prompt_tokens") or 0),
            "pending_completion_tokens": int(prev.get("pending_completion_tokens") or 0),
            "pending_total_tokens": int(prev.get("pending_total_tokens") or 0),
            "updated_at": time.time(),
        }
        _RUN_LIVE_META[run_id] = meta
    # Phase/tool transitions are rare but UI-critical — force past the throttle so
    # the MCP call shows the moment execution starts, not after the next text push.
    _emit_run_live_update(run_id, force=True)

def _set_run_live_usage(
    run_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
):
    with _RUN_STATE_LOCK:
        prev = _RUN_LIVE_STATE.get(run_id) or {}
        meta = _RUN_LIVE_META.get(run_id) or {}
        _RUN_LIVE_STATE[run_id] = {
            "text": prev.get("text", ""),
            "reasoning": prev.get("reasoning", ""),
            "phase": prev.get("phase", "generating"),
            "current_tool": prev.get("current_tool", ""),
            "pending_prompt_tokens": max(0, int(prompt_tokens or 0)),
            "pending_completion_tokens": max(0, int(completion_tokens or 0)),
            "pending_total_tokens": max(0, int(total_tokens or 0)),
            "updated_at": time.time(),
        }
        _RUN_LIVE_META[run_id] = meta
    _emit_run_live_update(run_id)

def _clear_run_live_text(run_id: str):
    with _RUN_STATE_LOCK:
        _RUN_LIVE_STATE.pop(run_id, None)
        _RUN_LIVE_META.pop(run_id, None)

def _set_run_live_meta(run_id: str, **meta: object) -> None:
    with _RUN_STATE_LOCK:
        current = dict(_RUN_LIVE_META.get(run_id) or {})
        current.update({k: v for k, v in meta.items() if v is not None})
        _RUN_LIVE_META[run_id] = current

def _emit_run_live_update(run_id: str, force: bool = False) -> None:
    # ``force`` bypasses the 80ms throttle for low-frequency but UI-critical
    # state changes (e.g. entering ``waiting_mcp`` with a tool name). Without it,
    # a phase/tool transition emitted right after a text delta gets swallowed by
    # the throttle, so the tool-call bubble only surfaces on the NEXT text push —
    # which is why MCP calls appeared to lag until the following reply text.
    with _RUN_STATE_LOCK:
        live = dict(_RUN_LIVE_STATE.get(run_id) or {})
        meta = dict(_RUN_LIVE_META.get(run_id) or {})
        last_emit_at = float(meta.get("last_emit_at") or 0.0)
        now = time.time()
        if not force and now - last_emit_at < 0.08:
            meta["last_emit_at"] = last_emit_at
            _RUN_LIVE_META[run_id] = meta
            return
        meta["last_emit_at"] = now
        _RUN_LIVE_META[run_id] = meta
    user_id = meta.get("user_id")
    if user_id is None:
        return

    payload = {
        "run_id": run_id,
        "user_id": user_id,
        "text": str(live.get("text") or ""),
        "reasoning": str(live.get("reasoning") or ""),
        "phase": str(live.get("phase") or "generating"),
        "current_tool": str(live.get("current_tool") or ""),
        "prompt_tokens": int(live.get("pending_prompt_tokens") or 0),
        "completion_tokens": int(live.get("pending_completion_tokens") or 0),
        "total_tokens": int(live.get("pending_total_tokens") or 0),
        "updated_at": live.get("updated_at"),
    }

    async def _do_emit():
        try:
            await sio.emit("chat:run_live", payload, room=f"user_{int(user_id)}")
        except Exception:
            logger.exception(f"chat_live_emit {run_id} failed")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_emit())
    except RuntimeError:
        asyncio.run(_do_emit())

def _emit_run_done(
    *,
    run_id: str,
    user_id: Optional[int],
    status: str,
    error_message: Optional[str] = None,
    session_id: Optional[str] = None,
    ai_config_id: Optional[int] = None,
    ai_kind: Optional[str] = None,
) -> None:
    """Push a terminal ``chat:run_done`` event to the run owner's room.

    The streaming ``chat:run_live`` channel only carries the live ``phase``
    (generating / waiting_mcp); the real run status (completed / error /
    stopped) lives on the ChatRun row. This terminal event lets the web client
    stop relying on history polling to learn a run finished. Emit failures are
    logged only — they must never break status persistence (mirrors
    ``_emit_run_live_update``). Workers route this through the gateway relay via
    the ``_RemoteSio`` proxy automatically.
    """
    run_id = str(run_id or "").strip()
    if not run_id or user_id is None:
        return
    payload = {
        "run_id": run_id,
        "user_id": user_id,
        "status": str(status or ""),
        "error_message": error_message,
        "session_id": session_id,
        "ai_config_id": ai_config_id,
        "ai_kind": ai_kind,
    }

    async def _do_emit():
        try:
            await sio.emit("chat:run_done", payload, room=f"user_{int(user_id)}")
        except Exception:
            logger.exception(f"chat_run_done_emit {run_id} failed")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_emit())
    except RuntimeError:
        asyncio.run(_do_emit())

def _split_tags(raw: Optional[str]) -> Tuple[str, str]:
    text = str(raw or "")
    idx = text.find(STATE_PREFIX)
    if idx < 0:
        return text.strip(), ""
    base = text[:idx].rstrip().rstrip("|").strip()
    encoded = text[idx + len(STATE_PREFIX):].strip()
    return base, encoded

def _encode_tags_with_state(base_tags: str, state: Optional[dict]) -> str:
    base = (base_tags or "").strip()
    if not state:
        return base
    encoded = requests.utils.quote(json.dumps(state, ensure_ascii=False))
    return f"{base} | {STATE_PREFIX}{encoded}" if base else f"{STATE_PREFIX}{encoded}"

def _append_mcp_state_to_tags(existing_tags: Optional[str], tool: str, arguments: dict, result_text: str) -> str:
    state = {"signatures": {_block_signature(tool, arguments): {"applied": True, "result": result_text}}}
    base, encoded = _split_tags(existing_tags)
    if encoded:
        try:
            decoded = requests.utils.unquote(encoded)
            existing = json.loads(decoded)
            if isinstance(existing, dict):
                signatures = existing.get("signatures")
                if not isinstance(signatures, dict):
                    signatures = {}
                    existing["signatures"] = signatures
                signatures.update(state["signatures"])
                state = existing
        except Exception:
            pass
    return _encode_tags_with_state(base, state)
