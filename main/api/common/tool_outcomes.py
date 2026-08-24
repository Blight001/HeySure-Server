"""Pure helpers for explicit tool-level success and failure envelopes."""

from __future__ import annotations

from typing import Any


def _first_text(*values: Any) -> str:
    for value in values:
        rendered = str(value or "").strip()
        if rendered:
            return rendered
    return ""


def find_reported_failure(value: Any, *, max_depth: int = 6) -> dict[str, Any] | None:
    """Return the most specific ``success=false`` object on a result chain.

    Endpoint results can be wrapped by MCP, Connector, and runtime transport
    envelopes. Only the conventional ``result`` chain is traversed so an
    unrelated failure inside a collection cannot change the call outcome.
    """

    current = value
    failure = None
    for _ in range(max(0, int(max_depth)) + 1):
        if not isinstance(current, dict):
            break
        if current.get("success") is False:
            failure = current
        current = current.get("result")
    return failure


def reported_failure_code(failure: dict[str, Any], default: str = "DEVICE_TOOL_FAILED") -> str:
    return _first_text(
        failure.get("errorCode"),
        failure.get("code"),
        failure.get("failure_type"),
        default,
    )[:120]


def reported_failure_message(
    failure: dict[str, Any],
    default: str = "Tool returned success=false",
) -> str:
    return _first_text(
        failure.get("error"),
        failure.get("message"),
        failure.get("summary"),
        failure.get("stderr"),
        failure.get("output"),
        failure.get("failure_type"),
        default,
    )[:2000]


def reported_failure_detail(
    failure: dict[str, Any],
    default: str = "Tool returned success=false",
) -> str:
    """Build a bounded, user-readable detail while preserving stable codes."""

    message = reported_failure_message(failure, default)
    failure_type = _first_text(failure.get("failure_type"))
    if failure_type:
        exit_code = failure.get("exit_code")
        prefix = (
            f"{failure_type} (exit_code={exit_code})"
            if exit_code is not None
            else failure_type
        )
    else:
        prefix = _first_text(failure.get("errorCode"), failure.get("code"))
    if not prefix or message == prefix or message.startswith(f"{prefix}:"):
        return message[:2000]
    return f"{prefix}: {message}"[:2000]
