"""Canonical token-usage arithmetic shared by persistence and reporting."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping

from sqlalchemy import case, func


def nonnegative_token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _ordering_value(value: Any) -> float:
    """Parse a persisted ordering field without truncating timestamps."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def canonical_token_counts(
    prompt_tokens: Any,
    completion_tokens: Any,
    total_tokens: Any,
) -> tuple[int, int, int]:
    """Return a stable ``prompt, completion, total`` triplet.

    OpenAI-compatible gateways are not consistent about ``total_tokens``: some
    omit it, some return a stale value, and historical rows may contain totals
    altered independently of their components.  Whenever either component is
    present, the components are authoritative and total is their sum.  A
    provider total is only a fallback for legacy payloads that expose no split.
    """
    prompt = nonnegative_token_count(prompt_tokens)
    completion = nonnegative_token_count(completion_tokens)
    supplied_total = nonnegative_token_count(total_tokens)
    total = prompt + completion if prompt or completion else supplied_total
    return prompt, completion, total


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(usage or {})
    prompt, completion, total = canonical_token_counts(
        normalized.get("prompt_tokens"),
        normalized.get("completion_tokens"),
        normalized.get("total_tokens"),
    )
    normalized.update({
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    })
    return normalized


def canonical_message_total(message: Any) -> int:
    return canonical_token_counts(
        getattr(message, "prompt_tokens", 0),
        getattr(message, "completion_tokens", 0),
        getattr(message, "total_tokens", 0),
    )[2]


def active_context_message_total(message: Any) -> int:
    """Return tokens that still contribute to the live conversation context.

    Compression preserves the original usage components for accounting, while
    ``compressed_away`` rows have already been replaced by a summary and must
    not keep the automatic-compression threshold permanently exceeded.
    """
    tags = {
        tag.strip()
        for tag in str(getattr(message, "tags", "") or "").split(",")
        if tag.strip()
    }
    return 0 if "compressed_away" in tags else canonical_message_total(message)


def context_snapshot_total(message: Any) -> int:
    """Return the provider's latest input-context snapshot for one turn.

    ``prompt_tokens`` is the size of the complete provider input for that
    turn.  It must not be added to other turns when calculating the *current*
    context: every MCP continuation resends the system prompt, history and
    tool schemas, so summing these snapshots counts the same context repeatedly.
    Legacy rows without a split usage payload fall back to their canonical
    total.
    """
    prompt = nonnegative_token_count(getattr(message, "prompt_tokens", 0))
    if prompt > 0:
        return prompt
    return canonical_message_total(message)


def latest_context_total(messages: Iterable[Any]) -> int:
    """Return the newest active assistant input-context snapshot.

    Historical usage remains cumulative for billing/reporting.  This helper is
    only for the live context bar and automatic-compression threshold.
    """
    candidates = []
    for message in messages:
        if str(getattr(message, "role", "") or "") != "assistant":
            continue
        tags = {
            tag.strip()
            for tag in str(getattr(message, "tags", "") or "").split(",")
            if tag.strip()
        }
        if "compressed_away" in tags:
            continue
        if context_snapshot_total(message) <= 0:
            continue
        candidates.append(message)
    if not candidates:
        return 0
    latest = max(
        candidates,
        key=lambda item: (
            _ordering_value(getattr(item, "created_at", 0)),
            nonnegative_token_count(getattr(item, "id", 0)),
        ),
    )
    return context_snapshot_total(latest)


def latest_context_totals_by_session(messages: Iterable[Any]) -> dict[tuple[Any, str], int]:
    """Return the newest active context snapshot for each AI/session pair."""
    latest_rows: dict[tuple[Any, str], Any] = {}
    for message in messages:
        if str(getattr(message, "role", "") or "") != "assistant":
            continue
        if "compressed_away" in str(getattr(message, "tags", "") or ""):
            continue
        snapshot = context_snapshot_total(message)
        if snapshot <= 0:
            continue
        key = (getattr(message, "ai_config_id", None), str(getattr(message, "session_id", "") or ""))
        previous = latest_rows.get(key)
        if previous is None or (
            _ordering_value(getattr(message, "created_at", 0)), nonnegative_token_count(getattr(message, "id", 0))
        ) > (
            _ordering_value(getattr(previous, "created_at", 0)), nonnegative_token_count(getattr(previous, "id", 0))
        ):
            latest_rows[key] = message
    return {key: context_snapshot_total(message) for key, message in latest_rows.items()}


def canonical_total_sql(model: Any):
    prompt = func.coalesce(model.prompt_tokens, 0)
    completion = func.coalesce(model.completion_tokens, 0)
    components = prompt + completion
    return case(
        (components > 0, components),
        else_=func.coalesce(model.total_tokens, 0),
    )
