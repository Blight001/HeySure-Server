"""Subprocess helpers for the host repository updater."""

from __future__ import annotations

import os
import signal
import subprocess
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Type


def run_command(
    root: Path,
    command: list[str],
    timeout: float,
    error_type: Type[RuntimeError],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        raise error_type(f"command failed: {command[0]}")
    return result


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_streaming(
    root: Path,
    command: list[str],
    timeout: float,
    environment: dict[str, str] | None,
    log: Callable[[str], None],
    error_type: Type[RuntimeError],
) -> None:
    started_at = time.monotonic()
    group_options = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
        **group_options,
    )
    assert process.stdout is not None
    output: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        for line in process.stdout:
            output.put(line)
        output.put(None)

    threading.Thread(target=read_output, name="repo-updater-output", daemon=True).start()
    try:
        while True:
            if time.monotonic() - started_at > timeout:
                _kill_process_group(process)
                raise error_type(f"command timed out: {command[0]}")
            try:
                line = output.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            log(line)
        return_code = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            _kill_process_group(process)
    if return_code:
        raise error_type(f"command failed with exit code {return_code}: {command[0]}")
