"""Render a compact system-prompt directory from :class:`ScopedToolView`."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from api.services.mcp.capability_types import DevicePromptMetadata, ScopedToolView


TOOLBOX_WORKSPACE_GUIDANCE = (
    "执行位置：工具箱中的 workspace.* 只操作 HeySure 服务器上当前 AI 的独立工作区，"
    "不是用户电脑、浏览器插件、桌面端设备或宝塔主机的工作目录。"
    "同名文件或相同相对路径也不代表两端文件相同，必须根据用户提到的设备、主机和路径自主选择对应工具组。"
    "用户说“AI 工作区/工作区附件/Uploads/Screenshots/file_ref”或给出相对工作区路径时使用 workspace.*；"
    "用户说“服务器主机/测试服/生产环境/宝塔/Docker/Compose/服务配置”或给出服务器绝对路径时，"
    "使用 baota MCP（若本轮已提供）查看和修改，禁止用 workspace.run+command 猜测或代替服务器操作；"
    "用户说“本机/电脑/桌面/浏览器/下载目录/设备端”时使用对应端侧设备 MCP。"
    "只有已经进入服务器 AI 工作区的文件，才能由 AI 直接读取、用 workspace.file+manage 查看图片、"
    "注册 file_ref 或通过 message.send+to 发给用户；设备端文件必须先用端侧上传/同步能力复制到服务器工作区。"
)

DEVICE_WORKSPACE_GUIDANCE = (
    "执行位置：本组工具运行在该端侧设备；其 cwd、文件路径和工具箱中的服务器 AI 工作区彼此独立。"
    "端侧文件不能直接交给 workspace.file+manage 或 message.send+to，需先上传/同步到服务器 AI 工作区。"
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
