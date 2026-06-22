import locale
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.device_bindings import get_binding
from api.models import AIRuntimeStatus, AssistantAIConfig
from api.sio import agents
from mcp_runtime.mcp.core import generate_file_tree, get_project_root, safe_join


MAX_COMMAND_LENGTH = 8000
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
MAX_FILE_BYTES = 1_000_000
SHELL_CHOICES = {"auto", "cmd", "powershell", "pwsh", "none"}
BLOCKED_COMMAND_RE = re.compile(
    r'\b('
    r'format|diskpart|mountvol|bcdedit|regedit|'
    r'takeown|icacls|net\s+user|net\s+localgroup|'
    r'shutdown|restart-computer|stop-computer|'
    r'ssh|scp|ftp|telnet'
    r')\b',
    re.IGNORECASE,
)


def _ensure_inside_workspace(root: str, path: str) -> str:
    abs_root = os.path.abspath(root)
    abs_path = os.path.abspath(path)
    try:
        common = os.path.commonpath([abs_root, abs_path])
    except ValueError:
        common = ""
    if common != abs_root:
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    return abs_path


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_command_cwd(project_root: str, cwd: Optional[str], *, strict_workspace: bool = False) -> str:
    if not cwd:
        return project_root
    cwd_text = str(cwd).strip()
    if not cwd_text or cwd_text == ".":
        return project_root
    if os.path.isabs(cwd_text):
        resolved = os.path.abspath(os.path.expanduser(os.path.expandvars(cwd_text)))
        if strict_workspace:
            resolved = _ensure_inside_workspace(project_root, resolved)
        if not os.path.isdir(resolved):
            raise HTTPException(status_code=400, detail="cwd does not exist or is not a directory")
        return resolved

    resolved = safe_join(project_root, cwd_text)
    if not os.path.isdir(resolved):
        raise HTTPException(status_code=400, detail="cwd does not exist or is not a directory")
    return _ensure_inside_workspace(project_root, resolved)


def _validate_command(command: str) -> None:
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=400, detail="Missing command")
    if len(command) > MAX_COMMAND_LENGTH:
        raise HTTPException(status_code=400, detail="Command is too long")
    if "\x00" in command:
        raise HTTPException(status_code=400, detail="Command contains invalid characters")
    if BLOCKED_COMMAND_RE.search(command):
        raise HTTPException(status_code=403, detail="Command is blocked by the command safety policy")


def _coerce_shell(value: Any) -> str:
    shell = str(value or "auto").strip().lower()
    if shell not in SHELL_CHOICES:
        raise HTTPException(status_code=400, detail=f"Unsupported shell: {shell}")
    return shell


def _validate_argv(argv: Any) -> List[str]:
    if not isinstance(argv, list) or not argv:
        raise HTTPException(status_code=400, detail="argv must be a non-empty array")
    parts = [str(item) for item in argv]
    if any(not part for part in parts):
        raise HTTPException(status_code=400, detail="argv must not contain empty items")
    command_text = " ".join(parts)
    _validate_command(command_text)
    return parts


def _build_command_invocation(command: str, shell: str) -> Tuple[Any, bool, str, str]:
    if shell == "none":
        raise HTTPException(status_code=400, detail="shell=none requires argv")
    if shell == "auto":
        return command, True, os.environ.get("COMSPEC") if os.name == "nt" else os.environ.get("SHELL", "/bin/sh"), "auto"
    if shell == "cmd":
        if os.name != "nt":
            raise HTTPException(status_code=400, detail="shell=cmd is only available on Windows")
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", command], False, comspec, "cmd"
    if shell in {"powershell", "pwsh"}:
        executable = "powershell.exe" if shell == "powershell" else "pwsh"
        return [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command], False, executable, shell
    raise HTTPException(status_code=400, detail=f"Unsupported shell: {shell}")


def _sandbox_env(project_root: str) -> Dict[str, str]:
    sandbox_home = os.path.join(project_root, ".sandbox_home")
    sandbox_tmp = os.path.join(project_root, ".sandbox_tmp")
    os.makedirs(sandbox_home, exist_ok=True)
    os.makedirs(sandbox_tmp, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "TEMP": sandbox_tmp,
        "TMP": sandbox_tmp,
        "USERPROFILE": sandbox_home,
        "HOME": sandbox_home,
        "SANDBOX_ROOT": project_root,
    }
    return {key: value for key, value in env.items() if value}


def _command_env(project_root: str, *, sandbox_env: bool = False) -> Dict[str, str]:
    if sandbox_env:
        return _sandbox_env(project_root)
    env = os.environ.copy()
    env.setdefault("SANDBOX_ROOT", project_root)
    return env


def _output_decode_encodings() -> List[str]:
    """Preferred decode order for bytes captured from a child process.

    Most cross-platform tools (git, node, python, PowerShell configured for a
    UTF-8 output encoding) emit UTF-8, so we try that first. On Windows,
    ``cmd.exe`` and native console programs instead emit the console/OEM code
    page — ``cp936``/GBK on a zh-CN system — which is exactly why decoding their
    output as UTF-8 turns Chinese text into mojibake (乱码). Fall back to the
    OEM/ANSI code page and the locale's preferred encoding so those byte streams
    decode correctly too.
    """
    encodings = ["utf-8"]
    if os.name == "nt":
        encodings += ["oem", "mbcs"]
    try:
        preferred = locale.getpreferredencoding(False)
    except Exception:
        preferred = ""
    if preferred:
        encodings.append(preferred)
    return encodings


def _decode_output(data: Any) -> str:
    """Decode captured stdout/stderr bytes, tolerating mixed encodings.

    ``subprocess`` gives us bytes here on purpose: a hard-coded ``encoding`` is
    what produced the garbled Chinese in the first place. We try a small set of
    candidate encodings (UTF-8 first, then the Windows OEM/locale code page) and
    only fall back to lossy replacement if none decode cleanly.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    seen: set[str] = set()
    for enc in _output_decode_encodings():
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _coerce_timeout(value: Any) -> int:
    try:
        seconds = int(value or DEFAULT_COMMAND_TIMEOUT)
    except Exception:
        seconds = DEFAULT_COMMAND_TIMEOUT
    return max(1, min(MAX_COMMAND_TIMEOUT, seconds))


def _resolve_workspace_file(project_root: str, args: Dict[str, Any]) -> str:
    target = args.get("target") if isinstance(args.get("target"), dict) else {}
    raw_path = args.get("path") or target.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail="Missing path")
    path_text = os.path.expanduser(os.path.expandvars(raw_path.strip()))
    if os.path.isabs(path_text):
        return _ensure_inside_workspace(project_root, path_text)
    return _ensure_inside_workspace(project_root, safe_join(project_root, path_text.replace("\\", "/")))


def _read_text_file(path: str, *, encoding: str = "utf-8", max_bytes: int = MAX_FILE_BYTES) -> Tuple[str, bool]:
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    size = os.path.getsize(path)
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"File too large ({size} bytes > {max_bytes})")
    with open(path, "r", encoding=encoding, errors="replace", newline="") as fh:
        return fh.read(), size > max_bytes


def _atomic_write_text(path: str, text: str, *, encoding: str = "utf-8", create_dirs: bool = False) -> None:
    parent = os.path.dirname(path)
    if create_dirs:
        os.makedirs(parent, exist_ok=True)
    if not os.path.isdir(parent):
        raise HTTPException(status_code=400, detail="Parent directory does not exist")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _resolve_content_text(args: Dict[str, Any]) -> str:
    content = args.get("content")
    if isinstance(content, dict):
        value = content.get("text")
    else:
        value = args.get("text")
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="Missing text content")
    return value


def _read_file(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    path = _resolve_workspace_file(project_root, args)
    max_bytes = int(args.get("max_bytes") or MAX_FILE_BYTES)
    text, truncated = _read_text_file(path, max_bytes=max(1, min(MAX_FILE_BYTES, max_bytes)))
    return {
        "success": True,
        "path": os.path.relpath(path, project_root).replace(os.sep, "/"),
        "abs_path": path,
        "text": text,
        "bytes": os.path.getsize(path),
        "truncated": truncated,
    }


def _write_file(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    path = _resolve_workspace_file(project_root, args)
    options = args.get("options") if isinstance(args.get("options"), dict) else {}
    create = _truthy(args.get("create") or options.get("create"))
    overwrite = _truthy(args.get("overwrite") or options.get("overwrite"))
    create_dirs = _truthy(args.get("create_dirs") or options.get("create_dirs"))
    exists = os.path.exists(path)
    if exists and not overwrite:
        raise HTTPException(status_code=409, detail="File exists; pass overwrite=true to replace it")
    if not exists and not create:
        raise HTTPException(status_code=404, detail="File does not exist; pass create=true to create it")
    text = _resolve_content_text(args)
    _atomic_write_text(path, text, create_dirs=create_dirs)
    return {
        "success": True,
        "path": os.path.relpath(path, project_root).replace(os.sep, "/"),
        "abs_path": path,
        "bytes": os.path.getsize(path),
        "created": not exists,
        "overwritten": exists,
    }


def _apply_edit(text: str, edit: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    op = str(edit.get("op") or edit.get("mode") or "replace").strip().lower()
    if op in {"replace", "delete"}:
        search = edit.get("search")
        if not isinstance(search, str) or search == "":
            raise HTTPException(status_code=400, detail=f"{op} edit requires non-empty search")
        effective_search = search
        count = text.count(search)
        if count == 0 and "\n" in search and "\r\n" not in search:
            crlf_search = search.replace("\n", "\r\n")
            crlf_count = text.count(crlf_search)
            if crlf_count:
                effective_search = crlf_search
                count = crlf_count
        if count == 0:
            raise HTTPException(status_code=409, detail=f"Search text not found: {search[:80]}")
        replace_all = _truthy(edit.get("replace_all"))
        if count > 1 and not replace_all:
            raise HTTPException(status_code=409, detail=f"Search text matched {count} times; pass replace_all=true or use a more specific block")
        replacement = "" if op == "delete" else str(edit.get("replace") if edit.get("replace") is not None else edit.get("text") or "")
        if effective_search != search and "\n" in replacement and "\r\n" not in replacement:
            replacement = replacement.replace("\n", "\r\n")
        limit = -1 if replace_all else 1
        return text.replace(effective_search, replacement, limit), {"op": op, "matches": count, "applied": count if replace_all else 1}
    if op in {"append", "prepend"}:
        value = edit.get("text")
        if not isinstance(value, str):
            value = str(edit.get("replace") or "")
        return (text + value if op == "append" else value + text), {"op": op, "applied": 1}
    raise HTTPException(status_code=400, detail="Unsupported edit op; use replace, delete, append, or prepend")


def _edit_file(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    path = _resolve_workspace_file(project_root, args)
    options = args.get("options") if isinstance(args.get("options"), dict) else {}
    create_if_missing = _truthy(args.get("create_if_missing") or options.get("create_if_missing"))
    exists = os.path.exists(path)
    if exists:
        text, _ = _read_text_file(path)
    elif create_if_missing:
        text = ""
    else:
        raise HTTPException(status_code=404, detail="File not found; pass create_if_missing=true to create it")
    raw_edits = args.get("edits")
    if not isinstance(raw_edits, list):
        raw_edits = [args]
    edits = [item for item in raw_edits if isinstance(item, dict)]
    if not edits:
        raise HTTPException(status_code=400, detail="Missing edits")
    original = text
    applied: List[Dict[str, Any]] = []
    for edit in edits:
        text, info = _apply_edit(text, edit)
        applied.append(info)
    if text == original and exists:
        return {
            "success": True,
            "changed": False,
            "path": os.path.relpath(path, project_root).replace(os.sep, "/"),
            "applied": applied,
        }
    _atomic_write_text(path, text, create_dirs=create_if_missing)
    return {
        "success": True,
        "changed": True,
        "path": os.path.relpath(path, project_root).replace(os.sep, "/"),
        "abs_path": path,
        "bytes": os.path.getsize(path),
        "created": not exists,
        "applied": applied,
    }


def _list_tree(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """Render the workspace file tree, optionally rooted at a sub-path."""
    project_root = get_project_root(user_id, ai_config_id)
    target = args.get("target") if isinstance(args.get("target"), dict) else {}
    sub = args.get("path") or target.get("path")
    if isinstance(sub, str) and sub.strip() and sub.strip() not in {".", "/"}:
        root = _resolve_workspace_file(project_root, {"path": sub})
    else:
        root = project_root
    return {
        "success": True,
        "root": os.path.relpath(root, project_root).replace(os.sep, "/") or ".",
        "abs_root": root,
        "tree": generate_file_tree(root),
    }


# Action → (handler, minimum role). ``None`` role means available to every tier.
_FILE_ACTIONS = {
    "read": (_read_file, None),
    "tree": (_list_tree, None),
    "write": (_write_file, "digital_member_manager"),
    "edit": (_edit_file, "digital_member_manager"),
}

_FILE_ACTION_ALIASES = {
    "list": "tree",
    "ls": "tree",
    "create": "write",
    "save": "write",
    "modify": "edit",
}


def _file_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    """Unified workspace file tool. Dispatch by ``action``.

    Folds ``workspace.{read_file,write_file,edit_file}`` plus a file-tree listing
    behind one ``action`` parameter. ``read``/``tree`` are open to every tier;
    ``write``/``edit`` stay manager+ (re-enforced here). Shell execution and web
    search remain separate tools (``workspace.run_command`` / ``workspace.search``).
    """
    from mcp_runtime.mcp.permissions import enforce_min_role

    raw = str((args or {}).get("action") or "").strip().lower()
    action = _FILE_ACTION_ALIASES.get(raw, raw)
    if not action:
        raise HTTPException(status_code=400, detail="action is required for workspace.manage")
    spec = _FILE_ACTIONS.get(action)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. 可用: {', '.join(sorted(_FILE_ACTIONS))}",
        )
    handler, min_role = spec
    if min_role:
        enforce_min_role(user_id, ai_config_id, min_role)
    return handler(user_id, args or {}, ai_config_id)


FILE_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(_FILE_ACTIONS),
            "description": (
                "操作类型："
                "read 读取工作区内文本文件；tree 列出工作区（或 path 子目录）的文件树；"
                "write 创建/覆盖文本文件（需管理者+）；edit 按文本块替换/删除/追加/前置编辑（需管理者+）。"
                "路径必须位于当前 AI 工作区内。"
            ),
        },
        "path": {"type": "string", "description": "目标文件/目录路径。相对路径相对当前 AI 工作区；绝对路径必须位于工作区内。"},
        "max_bytes": {"type": "integer", "description": "read：最多读取字节数，默认 1000000。"},
        "text": {"type": "string", "description": "write：要写入的完整文本。"},
        "create": {"type": "boolean", "description": "write：文件不存在时是否创建。"},
        "overwrite": {"type": "boolean", "description": "write：文件已存在时是否允许覆盖。"},
        "create_dirs": {"type": "boolean", "description": "write：是否自动创建父目录。"},
        "edits": {
            "type": "array",
            "items": {"type": "object"},
            "description": "edit：编辑列表。每项 op=replace/delete/append/prepend；replace/delete 需 search；多处匹配需 replace_all=true。",
        },
        "op": {"type": "string", "enum": ["replace", "delete", "append", "prepend"], "description": "edit：单条编辑写法的操作类型。"},
        "search": {"type": "string", "description": "edit：replace/delete 要查找的原文块（默认唯一匹配）。"},
        "replace": {"type": "string", "description": "edit：replace 的替换文本。"},
        "replace_all": {"type": "boolean", "description": "edit：是否替换/删除所有匹配，默认 false。"},
        "create_if_missing": {"type": "boolean", "description": "edit：文件不存在时是否按空文件创建。"},
    },
    "required": ["action"],
}


def _run_command(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    strict_workspace = _truthy(args.get("strict_workspace") or args.get("workspace_only"))
    sandbox_env = _truthy(args.get("sandbox_env") or args.get("isolated_env"))
    command_cwd = _resolve_command_cwd(project_root, args.get("cwd"), strict_workspace=strict_workspace)
    timeout = _coerce_timeout(args.get("timeout"))
    shell = _coerce_shell(args.get("shell"))
    dry_run = _truthy(args.get("dry_run") or args.get("preview"))
    argv = args.get("argv")
    if argv is not None:
        run_args = _validate_argv(argv)
        use_shell = False
        shell_used = "none"
        shell_executable = ""
        command_text = " ".join(run_args)
    else:
        command = args.get("command")
        _validate_command(command)
        command_text = str(command)
        run_args, use_shell, shell_executable, shell_used = _build_command_invocation(command_text, shell)

    base_result = {
        "cwd": command_cwd,
        "shell": shell_used,
        "shell_executable": shell_executable,
        "timeout": timeout,
        "command_length": len(command_text),
        "dry_run": dry_run,
    }
    if argv is not None:
        base_result["argv"] = run_args
    else:
        base_result["command"] = command_text
    if dry_run:
        return {
            "success": True,
            "failure_type": None,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "output": "",
            **base_result,
        }

    try:
        # Capture raw bytes (no ``text``/``encoding``) and decode ourselves:
        # Windows console programs emit the OEM code page (GBK on zh-CN), so a
        # hard-coded utf-8 decode is what garbled Chinese output. Decoding via
        # ``_decode_output`` (UTF-8 first, then the OEM/locale code page) also
        # handles tools that emit UTF-8 even when launched through cmd (git,
        # node, …), which a fixed per-shell encoding would still mangle.
        result = subprocess.run(
            run_args,
            shell=use_shell,
            cwd=command_cwd,
            env=_command_env(project_root, sandbox_env=sandbox_env),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_stdout = _decode_output(exc.stdout)
        timeout_stderr = _decode_output(exc.stderr)
        output = timeout_stdout
        if timeout_stderr:
            output += f"\nError:\n{timeout_stderr}"
        output += f"\nError:\nCommand timed out after {timeout} seconds"
        return {
            "success": False,
            "failure_type": "timeout",
            "exit_code": None,
            "stdout": timeout_stdout,
            "stderr": timeout_stderr,
            "output": output,
            **base_result,
        }
    except FileNotFoundError as exc:
        return {
            "success": False,
            "failure_type": "shell_launch_failed",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "output": f"Error:\n{exc}",
            **base_result,
        }
    except OSError as exc:
        return {
            "success": False,
            "failure_type": "shell_launch_failed",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "output": f"Error:\n{exc}",
            **base_result,
        }

    stdout = _decode_output(result.stdout)
    stderr = _decode_output(result.stderr)
    output = stdout
    if stderr:
        output += f"\nError:\n{stderr}"

    return {
        "success": result.returncode == 0,
        "failure_type": None if result.returncode == 0 else "nonzero_exit",
        "exit_code": result.returncode,
        "output": output,
        "stdout": stdout,
        "stderr": stderr,
        **base_result,
    }

def _parse_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _list_connected_socket_agents(
    user_id: Optional[int] = None,
    ai_config_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    expected_user_id = _parse_int(user_id)
    expected_ai_config_id = _parse_int(ai_config_id)
    for item in list(agents.values()):
        row = dict(item) if isinstance(item, dict) else {"value": item}
        agent_user_id = _parse_int(row.get("userId") or row.get("user_id"))
        if expected_user_id and agent_user_id and agent_user_id != expected_user_id:
            continue

        device_id = str(row.get("id") or "").strip()
        bound_ai_config_id = get_binding(expected_user_id, device_id) if expected_user_id and device_id else None
        if expected_ai_config_id and bound_ai_config_id != expected_ai_config_id:
            continue

        # 只暴露 AI 需要的字段，避免把整包 socket 元数据塞进工具结果。
        out.append({
            "id": device_id,
            "name": row.get("name") or row.get("deviceName") or device_id,
            "platform": row.get("platform") or row.get("os") or "",
            "ai_config_id": bound_ai_config_id,
            "source": "socket",
            "dispatchable": bound_ai_config_id is not None,
        })
    return out

def _list_managed_ai_agents(user_id: int) -> List[Dict[str, Any]]:
    with Session(engine) as session:
        cfgs = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user_id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()
        statuses = session.exec(
            select(AIRuntimeStatus).where(
                AIRuntimeStatus.user_id == user_id,
                AIRuntimeStatus.ai_kind == "assistant",
            )
        ).all()
    status_map = {int(row.ai_config_id): row for row in statuses if row.ai_config_id is not None}
    out: List[Dict[str, Any]] = []
    for cfg in cfgs:
        status = status_map.get(int(cfg.id or 0))
        current_status = str(status.current_status or "").strip() if status else ""
        out.append(
            {
                "id": f"ai_config_{cfg.id}",
                "ai_config_id": cfg.id,
                "name": cfg.name,
                "ai_role": cfg.ai_role,
                "digital_member_role": cfg.digital_member_role,
                "enabled": bool(cfg.enabled),
                "mcp_enabled": bool(cfg.mcp_enabled),
                "runtime_status": current_status or ("idle" if cfg.enabled else "stopped"),
                "runtime_tool": str(status.current_mcp_tool or "").strip() if status else "",
                "source": "ai_config",
                "dispatchable": False,
            }
        )
    return out

def _list_agents(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    # 端侧 agent（source=socket，dispatchable=true 可调度）与受管 AI 配置
    # （source=ai_config）合成一份列表，每条用 source/dispatchable 区分，无需再分三份。
    all_agents = _list_connected_socket_agents(user_id, ai_config_id) + _list_managed_ai_agents(user_id)
    return {
        "agents": all_agents,
        "agent_count": len(all_agents),
    }

def _get_overview(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    cfg_db_uri = None
    if ai_config_id:
        with Session(engine) as session:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.id == ai_config_id,
                )
            ).first()
            if cfg:
                cfg_db_uri = cfg.database_uri
    all_agents = _list_connected_socket_agents(user_id, ai_config_id) + _list_managed_ai_agents(user_id)
    return {
        "workspace_root": project_root,
        "workspace_tree": generate_file_tree(project_root),
        "database_uri": cfg_db_uri,
        "agent_count": len(all_agents),
        "agents": all_agents,
    }


# admin.manage 统一门面：用 action 选择 overview 系统总览 / list_agents 列出 Agent。
_ADMIN_ACTION_ALIASES = {
    "get_overview": "overview",
    "list": "list_agents",
    "agents": "list_agents",
}


def _admin_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    raw = str((args or {}).get("action") or "").strip().lower()
    action = _ADMIN_ACTION_ALIASES.get(raw, raw)
    if action == "overview":
        return _get_overview(user_id, args, ai_config_id)
    if action == "list_agents":
        return _list_agents(user_id, args, ai_config_id)
    raise HTTPException(
        status_code=400,
        detail="action is required for admin.manage（可用：overview / list_agents）",
    )

