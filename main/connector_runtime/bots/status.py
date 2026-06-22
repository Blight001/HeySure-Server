"""Shared status/diagnostics shaping for bot adapters.

Every adapter's ``build_status`` returned the same four-key dict
(``status`` / ``mode`` / ``label`` / ``message``) and re-derived the Chinese
label from the status string. That shape + label mapping is defined once here
so adapters only express their channel-specific *branching*, not the dict
plumbing (single-responsibility / don't-repeat-yourself).
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

#: Status string -> default UI label.
_LABELS: Dict[str, str] = {
    "success": "成功",
    "disabled": "未启用",
    "failed": "失败",
    "starting": "启动中",
}


def status_report(status: str, mode: str, message: str = "", *, label: Optional[str] = None) -> Dict[str, str]:
    """Build the uniform status dict, deriving ``label`` from ``status``."""
    return {
        "status": status,
        "mode": mode,
        "label": label if label is not None else _LABELS.get(status, "失败"),
        "message": message,
    }


def disabled(message: str) -> Dict[str, str]:
    return status_report("disabled", "off", message, label="未启用")


def failed(mode: str, message: str) -> Dict[str, str]:
    return status_report("failed", mode, message, label="失败")


def from_connection_state(
    state: Mapping[str, str],
    *,
    mode: str = "long_connection",
    starting_hint: str = "启动中",
) -> Dict[str, str]:
    """Translate a long-connection state map into a status report.

    ``starting_hint`` lets a channel surface a transient "启动中" label when its
    connection-state message contains that marker (QQ does this).
    """
    raw_status = str(state.get("status") or "failed")
    message = str(state.get("message") or "")
    if starting_hint and starting_hint in message:
        return status_report(raw_status, mode, message, label="启动中")
    label = "成功" if raw_status == "success" else "失败"
    return status_report(raw_status, mode, message, label=label)
