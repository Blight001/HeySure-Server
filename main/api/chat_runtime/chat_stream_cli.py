"""CLI provider: run a local agent CLI (e.g. grok) in headless streaming-json
mode and adapt its stdout into a StreamResult.

Model presets with ``provider == "cli"`` reach the worker as the sentinel
``base_url = "cli://<command>"`` (see api.services.model_presets). One
inference turn spawns one CLI process:

    <command> --prompt-file <tmp> --output-format streaming-json -m <model> ...

The CLI prints JSON Lines on stdout:
    {"type":"thought","data":"..."}   reasoning delta
    {"type":"text","data":"..."}      assistant text delta
    {"type":"end","stopReason":...}   turn finished

The conversation is stateless like the HTTP providers: the whole convo is
serialized into the prompt file on every turn (system prompt included, so the
command line stays short regardless of prompt size).
"""

import base64
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from api.core.config import DATA_DIR
from api.services.model_presets import cli_command_from_base_url
from .chat_prompt_utils import (
    _extract_first_complete_mcp_call,
    _set_run_live_phase,
    _set_run_live_reasoning,
    _set_run_live_text,
    _set_run_live_usage,
)
from .chat_runtime_helpers import _run_should_stop
from .chat_stream import StreamResult

CLI_TIMEOUT_SECONDS = 600
CLI_RUNTIME_DIR = os.path.join(DATA_DIR, "cli_runtime")
CLI_IMAGE_MAX_BYTES = 20 * 1024 * 1024

# grok refuses to build a session with zero built-in tools. Keep the harmless
# todo tool plus read_file: CLI image inputs are materialized as short-lived
# files in CLI_RUNTIME_DIR, and multimodal Grok models inspect their pixels via
# read_file. Web search and subagents remain disabled; the platform's own MCP
# tools flow through the <mcp-call> text protocol instead.
CLI_FIXED_ARGS = [
    "--output-format",
    "streaming-json",
    "--verbatim",
    "--tools",
    "todo_write,read_file",
    "--disable-web-search",
    "--no-subagents",
]

# The real (potentially huge) system prompt lives inside the prompt file; this
# short wrapper is all that goes on the command line.
CLI_SYSTEM_WRAPPER = (
    "你不是编程助手。接下来的输入由两部分组成：[系统设定] 与 [对话记录]。"
    "请完全遵循 [系统设定] 中的全部要求与角色设定，以助手身份直接回复"
    " [对话记录] 中最后一条消息。不要输出角色前缀，不要复述对话记录。"
)

_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "tool": "Tool Result"}

_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|jpg|webp|gif));base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _image_source_from_block(block: Dict[str, Any]) -> str:
    """Return a data URL, HTTP URL, or local path from a common image block."""
    btype = str(block.get("type") or "").lower()
    if btype == "image_url":
        value = block.get("image_url")
        if isinstance(value, dict):
            return str(value.get("url") or "").strip()
        return str(value or "").strip()
    if btype != "image":
        return ""

    source = block.get("source")
    if isinstance(source, dict):
        source_type = str(source.get("type") or "").lower()
        data = str(source.get("data") or "").strip()
        media_type = str(source.get("media_type") or source.get("mime_type") or "image/png")
        if source_type == "base64" and data:
            return f"data:{media_type};base64,{data}"
        return str(source.get("url") or source.get("path") or "").strip()

    value = block.get("data") or block.get("url") or block.get("path")
    if value and block.get("mimeType") and not str(value).startswith(("data:", "http://", "https://")):
        return f"data:{block.get('mimeType')};base64,{value}"
    return str(value or "").strip()


def _image_suffix(media_type: str, source: str = "") -> str:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(normalized)
    if suffix:
        return suffix
    path_suffix = os.path.splitext(urllib.parse.urlparse(source).path)[1].lower()
    return path_suffix if path_suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"


def _write_cli_image(
    data: bytes,
    suffix: str,
    temporary_paths: Optional[List[str]] = None,
) -> str:
    if not data or len(data) > CLI_IMAGE_MAX_BYTES:
        return ""
    image_file = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=suffix,
        prefix="image_",
        dir=CLI_RUNTIME_DIR,
        delete=False,
    )
    try:
        image_file.write(data)
    finally:
        image_file.close()
    image_path = os.path.abspath(image_file.name)
    if temporary_paths is not None:
        temporary_paths.append(image_path)
    return image_path


def _materialize_cli_image(
    block: Dict[str, Any],
    temporary_paths: Optional[List[str]] = None,
) -> str:
    """Materialize an OpenAI/Anthropic/ACP image block for Grok read_file."""
    source = _image_source_from_block(block)
    if not source:
        return ""

    if os.path.isfile(source):
        return os.path.abspath(source)
    if source.lower().startswith("file://"):
        local_path = urllib.request.url2pathname(urllib.parse.urlparse(source).path)
        if os.name == "nt" and local_path.startswith("/") and len(local_path) > 2 and local_path[2] == ":":
            local_path = local_path[1:]
        if os.path.isfile(local_path):
            return os.path.abspath(local_path)

    match = _DATA_IMAGE_RE.match(source)
    if match:
        encoded = re.sub(r"\s+", "", match.group(2))
        if len(encoded) > ((CLI_IMAGE_MAX_BYTES * 4) // 3) + 8:
            return ""
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            return ""
        return _write_cli_image(
            data,
            _image_suffix(match.group(1)),
            temporary_paths=temporary_paths,
        )

    if source.startswith(("http://", "https://")):
        try:
            request = urllib.request.Request(source, headers={"User-Agent": "HeySure-AI/2.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                media_type = str(response.headers.get_content_type() or "").lower()
                if not media_type.startswith("image/"):
                    return ""
                declared_size = int(response.headers.get("Content-Length") or 0)
                if declared_size > CLI_IMAGE_MAX_BYTES:
                    return ""
                data = response.read(CLI_IMAGE_MAX_BYTES + 1)
            return _write_cli_image(
                data,
                _image_suffix(media_type, source),
                temporary_paths=temporary_paths,
            )
        except Exception:
            return ""
    return ""


def _content_to_text(content: Any, image_paths: Optional[List[str]] = None) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
            elif btype in ("image_url", "image"):
                image_path = _materialize_cli_image(block, temporary_paths=image_paths)
                if image_path:
                    parts.append(
                        "[图片附件]\n"
                        f"图片绝对路径：{image_path}\n"
                        "必须使用 read_file 查看这张图片的像素内容，再基于实际画面继续。"
                    )
                else:
                    parts.append("[图片附件读取失败：CLI 适配层未能解析或下载该图片。]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _serialize_convo(convo: List[Dict], image_paths: Optional[List[str]] = None) -> str:
    """Flatten an OpenAI-format convo into a role-labelled transcript with the
    merged system prompt embedded up front."""
    system_parts: List[str] = []
    lines: List[str] = []
    for msg in convo or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        text = _content_to_text(msg.get("content"), image_paths=image_paths)
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if not text and not msg.get("tool_calls"):
            continue
        label = _ROLE_LABELS.get(role, role or "User")
        lines.append(f"{label}: {text}".rstrip())
    prompt_parts: List[str] = []
    if system_parts:
        prompt_parts.append("[系统设定]\n" + "\n\n".join(system_parts))
    prompt_parts.append("[对话记录]\n" + ("\n\n".join(lines) if lines else "User: （无内容）"))
    return "\n\n".join(prompt_parts)


def _resolve_cli_argv(base_url: str) -> List[str]:
    command = cli_command_from_base_url(base_url)
    if not command:
        raise RuntimeError("CLI 模型未配置命令路径，请在模型预设中填写 CLI 命令")
    argv = [tok.strip('"') for tok in shlex.split(command, posix=(os.name != "nt"))]
    argv = [tok for tok in argv if tok]
    if not argv:
        raise RuntimeError("CLI 模型未配置命令路径，请在模型预设中填写 CLI 命令")
    exe = argv[0]
    resolved = shutil.which(exe)
    if resolved is None and not os.path.isfile(exe):
        raise RuntimeError(
            f"CLI 命令未找到：{exe}。请确认服务器已安装该 CLI 并在模型预设中填写完整路径"
            "（Docker/远程部署环境通常没有本机 CLI，不支持 CLI 模型）"
        )
    argv[0] = resolved or exe
    return argv


def _reader_thread(pipe, out_queue: "queue.Queue[Optional[bytes]]") -> None:
    try:
        for raw in iter(pipe.readline, b""):
            out_queue.put(raw)
    except Exception:
        pass
    finally:
        out_queue.put(None)


def _stderr_thread(pipe, sink: List[bytes]) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            sink.append(raw)
            # Keep only the tail; error messages we surface are short.
            if len(sink) > 50:
                del sink[:-50]
    except Exception:
        pass


def _kill_quietly(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _unlink_paths_quietly(paths: List[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def stream_turn_cli(
    run_id: str,
    base_url: str,
    model: str,
    convo: List[Dict],
    native_tool_name_map: Dict[str, str],
) -> StreamResult:
    """Stream one turn through a local CLI in streaming-json mode.

    Mirrors stream_turn_openai_compat's contract: live text/reasoning pushes,
    <mcp-call> text-protocol extraction, stop-request handling, and a
    StreamResult return. Raises RuntimeError with a user-facing message when
    the CLI is missing or exits without producing output.
    """
    argv = _resolve_cli_argv(base_url)

    os.makedirs(CLI_RUNTIME_DIR, exist_ok=True)
    temporary_images: List[str] = []
    prompt_text = _serialize_convo(convo, image_paths=temporary_images)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix="prompt_",
        dir=CLI_RUNTIME_DIR,
        delete=False,
    )
    try:
        prompt_file.write(prompt_text)
    finally:
        prompt_file.close()

    full_argv = argv + [
        "--prompt-file",
        prompt_file.name,
        "--system-prompt-override",
        CLI_SYSTEM_WRAPPER,
        "--cwd",
        CLI_RUNTIME_DIR,
    ] + CLI_FIXED_ARGS
    if str(model or "").strip():
        full_argv += ["-m", str(model).strip()]

    sr = StreamResult()
    last_push_at = 0.0

    # Same per-turn live-state reset as stream_turn_openai_compat.
    _set_run_live_text(run_id, "")
    _set_run_live_reasoning(run_id, "")
    _set_run_live_phase(run_id, "generating")
    _set_run_live_usage(run_id, 0, 0, 0)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            full_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=CLI_RUNTIME_DIR,
            creationflags=creationflags,
        )
    except OSError as exc:
        _unlink_paths_quietly([prompt_file.name, *temporary_images])
        raise RuntimeError(f"CLI 启动失败：{exc}") from exc

    stdout_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
    stderr_tail: List[bytes] = []
    threading.Thread(
        target=_reader_thread, args=(proc.stdout, stdout_queue), daemon=True
    ).start()
    threading.Thread(
        target=_stderr_thread, args=(proc.stderr, stderr_tail), daemon=True
    ).start()

    deadline = time.time() + CLI_TIMEOUT_SECONDS
    try:
        while True:
            if _run_should_stop(run_id):
                _kill_quietly(proc)
                _set_run_live_text(run_id, "")
                sr.stopped = True
                return sr
            if time.time() > deadline:
                _kill_quietly(proc)
                raise RuntimeError(
                    f"CLI 推理超时（超过 {CLI_TIMEOUT_SECONDS} 秒），进程已终止"
                )
            try:
                item = stdout_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            line = item.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")

            if etype == "thought":
                data = str(event.get("data") or "")
                if data:
                    sr.reasoning_content += data
                    _set_run_live_reasoning(run_id, sr.reasoning_content)
            elif etype == "text":
                if sr.payload_call:
                    continue
                sr.assistant_text += str(event.get("data") or "")
                parsed_call, mcp_match = _extract_first_complete_mcp_call(sr.assistant_text)
                if parsed_call and mcp_match:
                    sr.assistant_text = sr.assistant_text[: mcp_match.end()]
                    sr.payload_call = parsed_call
                    _set_run_live_text(run_id, sr.assistant_text)
                    sr.finish_reason = sr.finish_reason or "mcp_wait"
                    _kill_quietly(proc)
                    break
                now = time.time()
                if (now - last_push_at) >= 0.05:
                    _set_run_live_text(run_id, sr.assistant_text)
                    last_push_at = now
            elif etype == "end":
                sr.finish_reason = sr.finish_reason or "stop"
            # Unknown event types (session bookkeeping etc.) are ignored.
    finally:
        _kill_quietly(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        try:
            os.unlink(prompt_file.name)
        except OSError:
            pass
        _unlink_paths_quietly(temporary_images)

    _set_run_live_text(run_id, sr.assistant_text)

    returncode = proc.poll()
    if (
        returncode not in (0, None)
        and not sr.assistant_text
        and not sr.payload_call
    ):
        stderr_text = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
        detail = stderr_text[-600:] if stderr_text else "（无错误输出）"
        raise RuntimeError(f"CLI 进程异常退出（退出码 {returncode}）：{detail}")

    if not sr.finish_reason:
        sr.finish_reason = "stop"
    return sr
