import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api.services.chat.conversation_compress import (
    CompressionRequest,
    _extract_summary_response,
    compress_session,
)


class _Response:
    status_code = 200
    reason = "OK"
    headers = {"content-type": "text/plain"}
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("no json")


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows, events):
        self.rows = rows
        self.events = events

    def exec(self, _statement):
        return _RowsResult(self.rows)

    def add(self, _row):
        self.events.append("add")

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


def _history_rows(count=6):
    return [
        SimpleNamespace(
            role="user" if index % 2 == 0 else "assistant",
            content=f"message-{index}",
            tags="",
            think=None,
            total_tokens=10,
        )
        for index in range(count)
    ]


def _compress(session, **kwargs):
    return compress_session(
        session,
        CompressionRequest(
            convo=[],
            user_id=7,
            ai_config_id=3,
            ai_kind="assistant",
            session_id="session-a",
            session_name="任务",
            model="model-a",
            api_key="key",
            base_url="http://model",
            system_prompt="system",
            compression_prompt="压缩：{history}",
            session_tokens=100,
            threshold=80,
            keep_recent=2,
            **kwargs,
        ),
    )


class ConversationCompressTests(unittest.TestCase):
    def test_non_json_response_reports_http_context(self):
        with self.assertRaisesRegex(RuntimeError, "non-JSON response"):
            try:
                _extract_summary_response(_Response())
            except RuntimeError as exc:
                self.assertIn("HTTP 200 OK", str(exc))
                self.assertIn("content-type=text/plain", str(exc))
                self.assertIn("body=<empty>", str(exc))
                raise

    def test_valid_response_extracts_summary(self):
        resp = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "摘要"}}]},
        )

        self.assertEqual(_extract_summary_response(resp), "摘要")

    def test_event_stream_response_extracts_delta_content(self):
        resp = SimpleNamespace(
            status_code=200,
            reason="OK",
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"content":"摘"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"要"}}]}\n\n'
                "data: [DONE]\n"
            ),
            raise_for_status=lambda: None,
        )

        self.assertEqual(_extract_summary_response(resp), "摘要")

    def test_event_stream_usage_only_returns_empty_summary(self):
        resp = SimpleNamespace(
            status_code=200,
            reason="OK",
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[],"usage":{"prompt_tokens":1}}\n\ndata: [DONE]\n',
            raise_for_status=lambda: None,
        )

        self.assertEqual(_extract_summary_response(resp), "")

    def test_tool_result_precedes_compression_boundary(self):
        events = []
        session = _Session(_history_rows(), events)

        def save_message(_session, _user_id, payload):
            events.append(("save", payload.tags))

        def post_summary(*_args, **_kwargs):
            events.append("request")
            self.assertEqual(
                _kwargs["headers"]["X-HeySure-History-Mode"],
                "stateless",
            )
            return SimpleNamespace(
                headers={"content-type": "application/json"},
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": "摘要"}}]},
            )

        with (
            patch("api.services.chat.conversation_compress._save_message", save_message),
            patch("api.services.chat.conversation_compress.ai_http_post", post_summary),
        ):
            rebuilt = _compress(
                session,
                on_tool_result=lambda success, _text: events.append(("tool", success)),
            )

        self.assertIsNotNone(rebuilt)
        self.assertLess(
            events.index(("tool", True)),
            events.index(("save", "conversation_summary,system_notice_compress_result")),
        )

    def test_short_history_still_compresses_one_message(self):
        events = []
        session = _Session(_history_rows(2), events)

        with (
            patch("api.services.chat.conversation_compress._save_message"),
            patch(
                "api.services.chat.conversation_compress.ai_http_post",
                return_value=SimpleNamespace(
                    headers={"content-type": "application/json"},
                    raise_for_status=lambda: None,
                    json=lambda: {"choices": [{"message": {"content": "摘要"}}]},
                ),
            ),
        ):
            rebuilt = _compress(session)

        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt[-1]["content"], "message-1")

    def test_failed_summary_adds_terminal_notice(self):
        events = []
        session = _Session(_history_rows(), events)

        def save_message(_session, _user_id, payload):
            events.append(("save", payload.tags))

        with (
            patch("api.services.chat.conversation_compress._save_message", save_message),
            patch(
                "api.services.chat.conversation_compress.ai_http_post",
                side_effect=RuntimeError("model unavailable"),
            ),
        ):
            tool_results = []
            rebuilt = _compress(
                session,
                on_tool_result=lambda success, text: tool_results.append((success, text)),
            )

        self.assertIsNone(rebuilt)
        self.assertEqual(
            [event for event in events if isinstance(event, tuple)],
            [
                ("save", "system_notice_compress_failed"),
            ],
        )
        self.assertEqual(tool_results[0][0], False)


if __name__ == "__main__":
    unittest.main()
