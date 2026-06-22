"""Chat-run shared state primitives.

Holds the in-process registries that follow a chat run across boundaries:
- ``_RUN_THREADS``       — worker thread by run_id (gateway, ai_runtime, connector bots all touch it)
- ``_RUN_LIVE_STATE``    — streaming token / phase snapshot, polled by HTTP and Socket.IO
- ``_RUN_LIVE_META``     — per-run metadata side-channel (run start ts, etc.)
- ``_RUN_STATE_LOCK``    — guards both live dicts under concurrent access
- ``STATE_PREFIX``       — sentinel prepended to MCP state payloads embedded in stream
- ``MAX_AUTO_SUPERVISION_ROUNDS`` — auto-supervision turn cap
- ``_AUTO_RUNTIME_SECTION_TITLES`` / ``_TASK_RUNTIME_SECTION_TITLES`` — prompt section
  titles the inference loop injects/strips around tool calls
- ``_TASK_CREATE_TOOL_NAMES`` — tool names that mark "this run kicked off a new task"

Lives under ``api.chat_runtime`` because the registries are infrastructure
shared by every process role (gateway routes, ai_runtime inference loop,
connector_runtime bot routers). HTTP-specific glue (``router = APIRouter()``)
stays with the chat router cluster.
"""

import threading
from typing import Any, Dict


STATE_PREFIX = "__HS_MCP_STATE__="

_RUN_THREADS: Dict[str, threading.Thread] = {}
_RUN_LIVE_STATE: Dict[str, Dict[str, object]] = {}
_RUN_LIVE_META: Dict[str, Dict[str, object]] = {}
# Optional per-run "live text observer" used to mirror streaming tokens to an
# external channel (e.g. a QQ streaming message). The value is an opaque object
# exposing ``update(text: str)`` — stored here so the shared chat-runtime layer
# can invoke it without importing any bot/connector code.
_RUN_STREAM_HOOKS: Dict[str, Any] = {}
_RUN_STATE_LOCK = threading.Lock()
MAX_AUTO_SUPERVISION_ROUNDS = 2
_AUTO_RUNTIME_SECTION_TITLES: tuple[str, ...] = (
    "AI 工作目录",
    "AI 数据库连接",
    "可用MCP工具",
    "动态 MCP 说明",
    # 旧版「全局MCP调用方法」曾被持久化进人格/管理 prompt，且含已删工具名
    # （workspace.read_file 等）。该段当前已不再注入，故在加载时一并剥离，
    # 让存量已落库的人格 prompt 就地自愈，无需数据迁移。
    "全局MCP调用方法",
    "任务运行时工作目录(绝对路径)",
    "任务运行时MCP调用规则",
    "任务运行时MCP工具白名单",
)
_TASK_RUNTIME_SECTION_TITLES: tuple[str, ...] = (
    "任务运行时工作目录(绝对路径)",
    "任务运行时MCP调用规则",
    "任务运行时MCP工具白名单",
)
_TASK_CREATE_TOOL_NAMES: set[str] = {
    "task.manage",
}


def register_run_stream(run_id: str, hook: Any) -> None:
    """Attach a live-text observer to ``run_id`` (see ``_RUN_STREAM_HOOKS``)."""
    run_id = str(run_id or "").strip()
    if not run_id or hook is None:
        return
    with _RUN_STATE_LOCK:
        _RUN_STREAM_HOOKS[run_id] = hook


def get_run_stream(run_id: str) -> Any:
    """Return the live-text observer registered for ``run_id``, or ``None``."""
    with _RUN_STATE_LOCK:
        return _RUN_STREAM_HOOKS.get(str(run_id or "").strip())


def pop_run_stream(run_id: str) -> Any:
    """Detach and return the live-text observer for ``run_id`` (or ``None``)."""
    with _RUN_STATE_LOCK:
        return _RUN_STREAM_HOOKS.pop(str(run_id or "").strip(), None)


def apply_relayed_run_live_state(payload: Any) -> bool:
    """Mirror an ai-runtime live snapshot into the gateway process.

    Split deployments keep separate Python heaps. The inference worker emits
    ``chat:run_live`` through the gateway's internal Socket.IO relay, so that
    relay is also the natural hand-off point for the polling APIs and AI-card
    endpoints that still read ``_RUN_LIVE_STATE`` locally.
    """
    if not isinstance(payload, dict):
        return False
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        return False

    try:
        updated_at = float(payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0

    with _RUN_STATE_LOCK:
        previous = _RUN_LIVE_STATE.get(run_id) or {}
        try:
            previous_updated_at = float(previous.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            previous_updated_at = 0.0
        if (
            updated_at
            and previous_updated_at
            and updated_at < previous_updated_at
        ):
            return False

        _RUN_LIVE_STATE[run_id] = {
            "text": str(payload.get("text") or ""),
            "reasoning": str(payload.get("reasoning") or ""),
            "phase": str(payload.get("phase") or "generating"),
            "current_tool": str(payload.get("current_tool") or ""),
            "pending_prompt_tokens": int(payload.get("prompt_tokens") or 0),
            "pending_completion_tokens": int(
                payload.get("completion_tokens") or 0
            ),
            "pending_total_tokens": int(payload.get("total_tokens") or 0),
            "updated_at": updated_at or payload.get("updated_at"),
        }
        meta = dict(_RUN_LIVE_META.get(run_id) or {})
        if payload.get("user_id") is not None:
            meta["user_id"] = payload["user_id"]
        _RUN_LIVE_META[run_id] = meta
    return True
