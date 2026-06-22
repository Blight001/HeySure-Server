import asyncio
import contextvars
import inspect
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.config import ai_workspace_dirname, user_workspace_dir
from api.database import engine
from api.models import AIRuntimeStatus, AssistantAIConfig
from api.sio import sio

MCP_INTROSPECTION_TOOLS = {"mcp.describe_tool"}
_IGNORED_WORKSPACE_DIRS = {".git", "__pycache__", "venv", "node_modules", ".aider"}

def _resolve_ai_workspace(user_id: int, ai_config_id: Optional[int]) -> str:
    """Resolve the working directory available to an AI.

    Manager digital members and assistant admins can manage the whole user
    workspace. Regular members stay restricted to their own ``<id>-<slug>``
    subdirectory. Callers that pass no ``ai_config_id`` get the user root.

    Note: the shared knowledge base is resolved separately
    (``user_shared_knowledge_dir``) so it stays one-per-user across AIs.
    """
    user_root = user_workspace_dir(user_id)

    if not ai_config_id:
        return user_root
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
    if not cfg:
        return user_root

    ai_role = str(cfg.ai_role or "").strip().lower()
    member_role = str(cfg.digital_member_role or "").strip().lower()
    if ai_role == "assistant_admin" or (
        ai_role == "digital_member" and member_role == "manager"
    ):
        return user_root

    return os.path.abspath(os.path.join(user_root, ai_workspace_dirname(cfg.id, cfg.name, cfg.ai_role)))

def get_project_root(user_id: int, ai_config_id: Optional[int] = None) -> str:
    workspace_dir = _resolve_ai_workspace(user_id, ai_config_id)
    os.makedirs(workspace_dir, exist_ok=True)
    return workspace_dir

def safe_join(root: str, *paths: str) -> str:
    abs_root = os.path.abspath(root)
    joined = os.path.abspath(os.path.join(abs_root, *paths))
    try:
        common = os.path.commonpath([abs_root, joined])
    except ValueError:
        common = ""
    if common != abs_root:
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    return joined

def generate_file_tree(path: str, prefix: str = "") -> str:
    tree_str = ""
    try:
        if not os.path.exists(path):
            return "Path not found\n"
        items = sorted(os.listdir(path))
        items = [i for i in items if i not in [".git", "__pycache__", "venv", "node_modules", ".aider"]]

        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            tree_str += f"{prefix}{connector}{item}\n"

            full_path = os.path.join(path, item)
            if os.path.isdir(full_path):
                new_prefix = prefix + ("    " if is_last else "│   ")
                tree_str += generate_file_tree(full_path, new_prefix)
    except Exception as exc:
        tree_str += f"{prefix}Error: {str(exc)}\n"
    return tree_str

@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[int, Dict[str, Any], Optional[int]], Any]
    destructive: bool = False

class MCPRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, MCPTool] = {}
        # Bumped every time the live tool set changes (initial load + reload).
        # Callers can cheaply cache derived tool catalogs keyed by version.
        self.version: int = 1

    def register(self, tool: MCPTool) -> None:
        self._tools[tool.name] = tool

    def replace_tools(self, new_tools: Dict[str, MCPTool]) -> None:
        """Atomically swap the live tool table.

        Keeps the registry object identity stable so callers that did
        ``from mcp_runtime.mcp.registry import registry`` see the updated tools
        through their existing reference. Version is bumped so that any
        cached payloads can be invalidated.
        """
        self._tools = dict(new_tools)
        self.version += 1

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "destructive": tool.destructive,
            }
            for tool in self._tools.values()
        ]

    def build_tools_payload(self, allowed_tools: Optional[set] = None) -> List[Dict[str, Any]]:
        tools = []
        for tool in self._tools.values():
            if allowed_tools is not None and tool.name not in allowed_tools:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return tools

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> MCPTool:
        tool = self._tools.get(name)
        if not tool:
            raise HTTPException(status_code=404, detail=f"Unknown MCP tool: {name}")
        return tool

    async def call(
        self,
        name: str,
        user_id: int,
        arguments: Optional[Dict[str, Any]] = None,
        ai_config_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        tool = self.get(name)
        args = arguments or {}
        _enforce_workshop_binding(tool.name, user_id, ai_config_id)
        await _set_runtime_status(user_id, ai_config_id, "running", tool.name)
        await _emit_mcp_status(user_id, ai_config_id, "running", tool.name)
        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await tool.handler(user_id, args, ai_config_id)
            else:
                # Sync handlers do blocking I/O. Use ``asyncio.to_thread`` so
                # short-lived loops (``asyncio.run`` / worker bridges) do not
                # lose executor callbacks with "Event loop is closed".
                ctx = contextvars.copy_context()
                try:
                    result = await asyncio.to_thread(
                        ctx.run, tool.handler, user_id, args, ai_config_id
                    )
                except RuntimeError as exc:
                    if "Event loop is closed" in str(exc):
                        # Delivery from the threadpool hit a closed caller loop
                        # (common after mixed asyncio.run + cached clients).
                        # Re-raise so caller sees a clean failure instead of
                        # losing the whole execution channel.
                        raise
                    raise
            if hasattr(result, "__await__"):
                result = await result
            payload = {
                "tool": tool.name,
                "destructive": tool.destructive,
                "result": result,
            }
            await _set_runtime_status(user_id, ai_config_id, "idle", tool.name)
            await _emit_mcp_status(user_id, ai_config_id, "idle", tool.name)
            return payload
        except Exception:
            await _set_runtime_status(user_id, ai_config_id, "error", tool.name)
            await _emit_mcp_status(user_id, ai_config_id, "error", tool.name)
            raise

def _enforce_workshop_binding(tool_name: str, user_id: int, ai_config_id: Optional[int]) -> None:
    """作坊绑定门禁：图书馆工具需绑定图书馆；工具箱工具需绑定工具箱。

    没有 ``ai_config_id`` 视为核心 / 管理员直调，放行（与 ``enforce_min_role``
    约定一致）。仅服务端固定工具经 ``MCPRegistry.call``，故两类门禁均按确切设备
    绑定逐次校验；自省工具（mcp.describe_tool）始终放行。
    """
    if not ai_config_id:
        return
    from .permissions import requires_library_binding

    if requires_library_binding(tool_name):
        from api.workshop_bindings import config_bound_to_library

        if not config_bound_to_library(user_id, ai_config_id):
            raise HTTPException(
                status_code=403,
                detail=f"该 AI 未绑定图书馆，无法调用 {tool_name}（请在 AI 配置或世界中绑定图书馆）",
            )
        return
    # 工具箱门禁判定与绑定逻辑由独立的「工具箱」设备模块负责。
    from tools.engine import enforce_toolbox_binding

    enforce_toolbox_binding(tool_name, user_id, ai_config_id)


def _write_runtime_status(user_id: int, ai_config_id: Optional[int], status: str, tool: str) -> None:
    with Session(engine) as session:
        row = session.exec(
            select(AIRuntimeStatus).where(
                AIRuntimeStatus.user_id == user_id,
                AIRuntimeStatus.ai_config_id == ai_config_id,
                AIRuntimeStatus.ai_kind == "assistant",
            )
        ).first()
        if not row:
            row = AIRuntimeStatus(
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind="assistant",
            )
        row.current_status = status
        row.current_mcp_tool = tool
        row.updated_at = time.time()
        session.add(row)
        session.commit()


async def _set_runtime_status(user_id: int, ai_config_id: Optional[int], status: str, tool: str) -> None:
    try:
        await asyncio.to_thread(
            _write_runtime_status, user_id, ai_config_id, status, tool
        )
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise


async def _emit_mcp_status(
    user_id: int,
    ai_config_id: Optional[int],
    state: str,
    tool: str,
) -> None:
    try:
        await sio.emit(
            "mcp:status",
            {
                "userId": user_id,
                "aiConfigId": ai_config_id,
                "state": state,
                "tool": tool,
                "updatedAt": time.time(),
            },
            room=f"user_{user_id}",
        )
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise
