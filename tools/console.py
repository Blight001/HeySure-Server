"""Unified, schema-first console entry point for the AI workspace.

``workspace.run+command`` remains the implementation primitive.  This tool is
the stable public contract used by the model and the console UI: the gateway
broker can route ``target=device`` to any authorized endpoint MCP (defaulting
to ``run_command``) while the MCP runtime executes the workspace target locally.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException

CONSOLE_EXECUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "enum": ["workspace", "device"],
            "description": "执行目标：workspace=当前 AI 服务器工作区；device=已授权的端侧 MCP。默认 workspace。",
        },
        "command": {"type": "string", "description": "shell 命令；与 argv 二选一。"},
        "argv": {
            "type": "array",
            "items": {"type": "string"},
            "description": "参数化命令，不经过 shell；与 command 二选一。",
        },
        "tool": {
            "type": "string",
            "description": "target=device 时可指定任意已授权的端侧 MCP 工具；省略时使用 run_command。",
        },
        "arguments": {
            "type": "object",
            "description": "target=device 且指定 tool 时传给该端侧 MCP 的结构化参数。",
        },
        "cwd": {"type": "string", "description": "workspace 内相对目录；target=device 时由端侧工具解释。"},
        "shell": {
            "type": "string",
            "enum": ["auto", "cmd", "powershell", "pwsh", "none"],
            "description": "命令解释器；argv 调用固定为 none。",
        },
        "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "description": "超时秒数。"},
        "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 1800000, "description": "端侧命令超时毫秒数。"},
        "strict_workspace": {"type": "boolean", "description": "拒绝 workspace 外的 cwd。"},
        "sandbox_env": {"type": "boolean", "description": "使用工作区隔离 HOME/TEMP。"},
        "dry_run": {"type": "boolean", "description": "仅解析命令，不执行。"},
    },
    "required": [],
}


def console_device_request(args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Extract an endpoint MCP name and arguments from the console envelope."""
    requested_tool = str((args or {}).get("tool") or "run_command").strip()
    nested = (args or {}).get("arguments")
    if isinstance(nested, dict):
        endpoint_args = dict(nested)
    else:
        endpoint_args = dict(args or {})
        for key in ("target", "tool", "device_id", "arguments"):
            endpoint_args.pop(key, None)
    return requested_tool, endpoint_args


def _console_execute(
    user_id: int,
    args: Dict[str, Any],
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    """Execute the workspace half of the console contract.

    Device execution is intercepted by the gateway/AI Tool Broker before this
    handler is reached.  Rejecting it here prevents an internal MCP caller from
    silently treating a device path as a server workspace path.
    """
    target = str((args or {}).get("target") or "workspace").strip().lower()
    if target != "workspace":
        raise HTTPException(
            status_code=400,
            detail="target=device must be routed through the authorized endpoint MCP",
        )
    forwarded = dict(args or {})
    forwarded.pop("target", None)
    forwarded.pop("device_id", None)
    # Importing ``tools.workspace`` directly before the MCP package is
    # initialized triggers the legacy registry bootstrap cycle.  Touch the
    # package first so the handler is also safe for standalone console tests.
    from mcp_runtime.mcp import registry as _registry  # noqa: F401
    from tools.workspace import _run_command

    return _run_command(user_id, forwarded, ai_config_id)

