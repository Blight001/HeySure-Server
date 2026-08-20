"""Canonical token-usage arithmetic shared by persistence and reporting."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import case, func


def nonnegative_token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


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


def canonical_total_sql(model: Any):
    prompt = func.coalesce(model.prompt_tokens, 0)
    completion = func.coalesce(model.completion_tokens, 0)
    components = prompt + completion
    return case(
        (components > 0, components),
        else_=func.coalesce(model.total_tokens, 0),
    )
