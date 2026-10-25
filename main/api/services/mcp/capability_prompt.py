"""Render a compact system-prompt directory from :class:`ScopedToolView`."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from api.services.mcp.capability_types import DevicePromptMetadata, ScopedToolView


TOOLBOX_WORKSPACE_GUIDANCE = (
    "Workspace MCP tools operate only inside the current HeySure AI workspace; they are not the user's computer, "
    "browser, desktop device, or Baota host. Use workspace.list+files(path/query) for bounded discovery and "
    "console.execute(target=workspace) for explicit commands. Do not use unrestricted recursive listings. "
    "Use workspace.file+manage(action=view_image) for server workspace images. "
    "For every cross-member transfer use workspace.transfer: prepare_download or prepare_upload, stream bytes "
    "through the capability URL, then commit uploads. Never pass absolute paths, public media URLs, or base64."
)

MEMBER_TRANSFER_GUIDANCE = (
    "Canonical member data transfer: use workspace.transfer with prepare_download or prepare_upload. "
    "Stream bytes through the returned capability URL using endpoint/console MCP, then commit uploads. "
    "Never put filesystem paths or base64 in MCP arguments or conversation text."
)

DEVICE_WORKSPACE_GUIDANCE = (
    "Endpoint MCP runs on the selected device and has its own cwd and file roots. "
    "Use workspace.transfer when moving bytes between the device and the server; do not pass device paths "
    "to server workspace tools or message.send+to."
)
DEVICE_WORKSPACE_GUIDANCE += " " + MEMBER_TRANSFER_GUIDANCE

# Keep the concrete boundary vocabulary in the catalog: it is useful to both
# humans and models, and prevents a device path from being mistaken for a
# server path while the transfer protocol stays canonical.
TOOLBOX_WORKSPACE_GUIDANCE += (
    " \u540c\u540d\u6587\u4ef6\u6216\u76f8\u540c\u76f8\u5bf9\u8def\u5f84\u4e5f\u4e0d\u4ee3\u8868\u4e24\u7aef\u6587\u4ef6\u76f8\u540c?"
    " \u4f7f\u7528 baota MCP \u67e5\u770b\u548c\u4fee\u6539\u670d\u52a1\u5668\u4e3b\u673a\u3002"
    " \u7528\u6237\u8bf4\u201cAI \u5de5\u4f5c\u533a/\u5de5\u4f5c\u533a\u9644\u4ef6/Uploads/Screenshots/file_ref\u201d \u65f6\u624d\u4f7f\u7528\u670d\u52a1\u5668\u5de5\u4f5c\u533a\u5de5\u5177\u3002"
    " \u7528\u6237\u8bf4\u201c\u672c\u673a/\u7535\u8111/\u684c\u9762/\u6d4f\u89c8\u5668/\u4e0b\u8f7d\u76ee\u5f55/\u8bbe\u5907\u7aef\u201d \u65f6\u4f7f\u7528\u5bf9\u5e94\u8bbe\u5907 MCP\u3002"
    " \u53ea\u6709\u5df2\u7ecf\u8fdb\u5165\u670d\u52a1\u5668 AI \u5de5\u4f5c\u533a\u7684\u6587\u4ef6\u624d\u53ef\u7531 AI \u76f4\u63a5\u8bfb\u53d6\u3002"
    " \u8bbe\u5907\u7aef\u6587\u4ef6\u5fc5\u987b\u5148\u7528\u7aef\u4fa7\u4e0a\u4f20/\u540c\u6b65\u80fd\u529b\u590d\u5236\u5230\u670d\u52a1\u5668\u5de5\u4f5c\u533a\u3002"
)
DEVICE_WORKSPACE_GUIDANCE += (
    " \u8bbe\u5907\u8bf4\u660e\uff1a\u7528\u4e8e\u64cd\u4f5c\u5df2\u767b\u5f55\u7684\u5185\u5bb9\u540e\u53f0\u3002"
    " \u672c\u7ec4\u5de5\u5177\u8fd0\u884c\u5728\u8be5\u7aef\u4fa7\u8bbe\u5907\u3002"
    " \u8bbe\u5907\u7aef\u6587\u4ef6\u5fc5\u987b\u5148\u7528\u7aef\u4fa7\u4e0a\u4f20/\u540c\u6b65\u80fd\u529b\u590d\u5236\u5230\u670d\u52a1\u5668 AI \u5de5\u4f5c\u533a\u3002"
    " \u9700\u5148\u4e0a\u4f20/\u540c\u6b65\u5230\u670d\u52a1\u5668 AI \u5de5\u4f5c\u533a\u3002"
)

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
    sections = [
        _section(
            "工具箱 MCP（服务器 AI 工作区）",
            toolbox_names,
            view,
            guidance=TOOLBOX_WORKSPACE_GUIDANCE,
        )
    ]
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
            guidance=DEVICE_WORKSPACE_GUIDANCE,
        ))
    return sections


def _section(
    label: str,
    names: Iterable[str],
    view: ScopedToolView,
    *,
    description: str = "",
    guidance: str = "",
) -> str:
    lines = [_tool_line(name, view) for name in sorted(set(names)) if name in view.eligible]
    heading = [label]
    normalized_description = " ".join(str(description or "").split())
    if normalized_description:
        heading.append(f"  设备说明：{normalized_description}")
    normalized_guidance = " ".join(str(guidance or "").split())
    if normalized_guidance:
        heading.append(f"  工作目录边界：{normalized_guidance}")
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
