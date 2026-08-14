"""Render a compact system-prompt directory from :class:`ScopedToolView`."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from api.services.mcp.capability_types import DevicePromptMetadata, ScopedToolView


def render_scoped_tool_catalog(
    view: ScopedToolView,
    *,
    user_id: int,
    ai_config_id: int | None,
) -> str:
    from mcp_runtime.mcp.permissions import LIBRARY_BOUND_TOOLS
    from api.services.mcp.mcp_prompt_groups import automation_card_prompt_sections

    library_names = set(LIBRARY_BOUND_TOOLS) & set(view.eligible_names)
    device_names = set().union(*view.device_tool_names.values()) if view.device_tool_names else set()
    toolbox_names = set(view.eligible_names) - library_names - device_names
    sections = [_section("工具箱 MCP", toolbox_names, view)]
    sections.extend(_device_sections(view))
    if library_names:
        sections.append(_section("图书馆 MCP", library_names, view))
    sections.extend(
        automation_card_prompt_sections(user_id, ai_config_id, set(view.eligible_names))
    )
    return "\n\n".join(section for section in sections if section) or "- （空）"


def _device_sections(view: ScopedToolView) -> list[str]:
    if not view.devices:
        return ["端侧设备 MCP\n- （当前无可用工具）"]
    labels = [_device_label(item) for item in view.devices]
    counts = Counter(labels)
    sections = []
    for metadata, base_label in zip(view.devices, labels):
        label = base_label
        if counts[base_label] > 1:
            suffix = metadata.device_id[-4:]
            label = f"{base_label}·{suffix}" if suffix else base_label
        names = view.device_tool_names.get(metadata.device_id, frozenset())
        sections.append(_section(
            f"{label} MCP",
            names,
            view,
            description=metadata.purpose,
        ))
    return sections


def _section(
    label: str,
    names: Iterable[str],
    view: ScopedToolView,
    *,
    description: str = "",
) -> str:
    lines = [_tool_line(name, view) for name in sorted(set(names)) if name in view.eligible]
    heading = [label]
    normalized_description = " ".join(str(description or "").split())
    if normalized_description:
        heading.append(f"  设备说明：{normalized_description}")
    heading.append("\n".join(lines) if lines else "- （当前无可用工具）")
    return "\n".join(heading)


def _tool_line(name: str, view: ScopedToolView) -> str:
    capability = view.eligible[name]
    marker = " !" if capability.destructive else ""
    description = _short_description(capability.description)
    return f"  - {name}{marker}: {description}" if description else f"  - {name}{marker}"


def _short_description(value: str, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _device_label(item: DevicePromptMetadata) -> str:
    if item.name and item.name.lower() != item.device_id.lower():
        return item.name
    return {
        "browser": "浏览器插件",
        "android": "安卓端",
        "custom": "自建设备",
        "workshop": "图书馆",
    }.get(item.device_type, "桌面端")
