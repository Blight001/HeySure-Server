"""Render a compact system-prompt directory from :class:`ScopedToolView`."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from api.services.mcp.capability_types import DevicePromptMetadata, ScopedToolView


MCP_FILE_TRANSFER_RULES = (
    "所有 MCP 共用：服务器→设备调用 workspace.transfer.prepare_download，"
    "设备使用返回的 transport.url 流式 GET 下载；设备→服务器调用 workspace.transfer.prepare_upload，"
    "设备使用 transport.url 流式 PUT 主动上传，随后调用 commit(bytes,sha256) 获取服务器 file_ref。"
    "只交换小型引用、校验信息和临时 URL；禁止在 MCP 或对话中传绝对路径、端侧路径、公网素材 URL 或 base64。"
)

TOOLBOX_WORKSPACE_GUIDANCE = (
    "执行位置：工具箱 workspace.* 只操作当前 AI 的 HeySure 服务器工作区，不是用户电脑、浏览器、端侧设备或宝塔主机。"
    "使用 workspace.list+files(path/query) 做有界发现，使用 console.execute(target=workspace) 执行明确命令；"
    "禁止无界递归列目录。服务器工作区图片使用 workspace.file+manage(action=view_image)。"
    "同名文件或相同相对路径不代表两端文件相同；服务器主机操作使用 baota MCP。"
)

DEVICE_WORKSPACE_GUIDANCE = (
    "执行位置：本组工具运行在所选端侧设备；其 cwd、文件路径与服务器 AI 工作区彼此独立。"
    "端侧文件不能直接交给服务器 workspace 工具或 message.send+to，必须先按统一传输流程传到服务器工作区。"
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
    if "workspace.transfer" in view.eligible or device_names:
        sections.append(f"文件传输最小合同\n  {MCP_FILE_TRANSFER_RULES}")
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
