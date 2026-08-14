"""MCP capability categories that are backed by built-in devices.

AI roles are presentation metadata only and do not participate in MCP
authorization.  Runtime access is resolved from the member's bound devices and
the per-member scope saved for each device.
"""

from typing import Set


# 图书馆设备承载的治理工具。调用 AI 必须绑定图书馆，并且该工具必须位于
# 图书馆针对该成员保存的 scope 中；成员角色不参与授权。其余服务端固定工具由
# 工具箱设备按同样的“绑定 + 成员 scope”规则授权。
LIBRARY_BOUND_TOOLS: Set[str] = {
    "member.manage",
    "device+mcp.manage",
    "knowledge.manage",
}


def requires_library_binding(tool_name: str) -> bool:
    """该工具是否需要「图书馆」绑定才能由 AI 调用。"""
    return str(tool_name or "").strip() in LIBRARY_BOUND_TOOLS


__all__ = ["LIBRARY_BOUND_TOOLS", "requires_library_binding"]
