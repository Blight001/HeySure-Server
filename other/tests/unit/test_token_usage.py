from types import SimpleNamespace

from api.chat_runtime import chat_runtime_helpers
from api.models import ChatMessage, TokenUsageSnapshot
from api.services.chat.token_usage import (
    active_context_message_total,
    canonical_message_total,
    canonical_token_counts,
    canonical_total_sql,
    normalize_usage,
)


def test_components_are_authoritative_when_provider_total_is_wrong():
    assert canonical_token_counts(120, 30, 70) == (120, 30, 150)
    assert canonical_token_counts(120, 30, 999) == (120, 30, 150)


def test_total_is_fallback_when_provider_exposes_no_components():
    assert canonical_token_counts(None, None, 45) == (0, 0, 45)


def test_negative_and_invalid_counts_are_never_persisted():
    assert canonical_token_counts(-2, "bad", 9) == (0, 0, 9)


def test_normalize_usage_preserves_provider_details():
    usage = normalize_usage({
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 3,
        "cache_read_input_tokens": 7,
    })

    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "cache_read_input_tokens": 7,
    }


def test_existing_message_total_is_repaired_at_read_time():
    row = SimpleNamespace(prompt_tokens=80, completion_tokens=5, total_tokens=12)
    assert canonical_message_total(row) == 85

    model_row = ChatMessage(user_id=1, role="assistant", content="x")
    model_row.prompt_tokens = 80
    model_row.completion_tokens = 5
    model_row.total_tokens = 12
    assert model_row.effective_total_tokens == 85

    snapshot = TokenUsageSnapshot(user_id=1, bucket="2026-08-20")
    snapshot.prompt_tokens = 80
    snapshot.completion_tokens = 5
    snapshot.total_tokens = 12
    assert snapshot.effective_total_tokens == 85


def test_compressed_rows_keep_accounting_usage_but_leave_active_context_total():
    compressed = SimpleNamespace(
        prompt_tokens=5_500_000,
        completion_tokens=30_000,
        total_tokens=0,
        tags="mcp_assistant_call, compressed_away",
    )
    active = SimpleNamespace(
        prompt_tokens=40_000,
        completion_tokens=2_000,
        total_tokens=1,
        tags="conversation_summary,system_notice_compress_result",
    )

    assert canonical_message_total(compressed) == 5_530_000
    assert active_context_message_total(compressed) == 0
    assert active_context_message_total(active) == 42_000


def test_session_threshold_excludes_compressed_rows_but_keeps_active_usage():
    compressed = SimpleNamespace(
        prompt_tokens=5_500_000,
        completion_tokens=30_000,
        total_tokens=0,
        tags="compressed_away",
    )
    active = SimpleNamespace(
        prompt_tokens=40_000,
        completion_tokens=2_000,
        total_tokens=1,
        tags="",
    )

    class Results:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Session:
        def __init__(self):
            self.calls = 0

        def exec(self, _statement):
            self.calls += 1
            return Results([compressed, active] if self.calls == 1 else [])

    assert chat_runtime_helpers._session_total_tokens(
        Session(), 1, "assistant", "session", 4,
    ) == 42_000


def test_sql_total_uses_components_before_legacy_total():
    sql = str(canonical_total_sql(ChatMessage)).lower()
    assert "case" in sql
    assert "prompt_tokens" in sql
    assert "completion_tokens" in sql
    assert "total_tokens" in sql
