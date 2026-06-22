import unittest
from types import SimpleNamespace

from api.services.conversation_compress import _extract_summary_response


class _Response:
    status_code = 200
    reason = "OK"
    headers = {"content-type": "text/plain"}
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("no json")


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


if __name__ == "__main__":
    unittest.main()
