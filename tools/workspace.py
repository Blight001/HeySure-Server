import locale
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.devices.bindings import get_binding
from api.models import AIRuntimeStatus, AssistantAIConfig
from api.sio import agents
from mcp_runtime.mcp.core import generate_file_tree, get_project_root, safe_join


MAX_COMMAND_LENGTH = 8000
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
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


def _caller_is_below_manager(user_id: int, ai_config_id: Optional[int]) -> bool:
    from mcp_runtime.mcp.permissions import ROLE_MANAGER, ROLE_RANK, config_role_tier

    if not ai_config_id:
        return False
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
    tier = config_role_tier(cfg) if cfg else "digital_member_member"
    return ROLE_RANK.get(tier, 0) < ROLE_RANK.get(ROLE_MANAGER, 0)


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


def _run_command(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    project_root = get_project_root(user_id, ai_config_id)
    strict_workspace = _truthy(args.get("strict_workspace") or args.get("workspace_only"))
    if _caller_is_below_manager(user_id, ai_config_id):
        strict_workspace = True
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
