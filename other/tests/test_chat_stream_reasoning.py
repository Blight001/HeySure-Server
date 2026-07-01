import unittest
from unittest import mock

from api.chat_runtime import chat_stream
from api.chat_runtime.chat_prompt_utils import (
    _clear_run_live_text,
    _get_run_live_reasoning,
)


class _FakeResponse:
    """Minimal stand-in for a streaming ``requests.Response``."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8")

    def close(self):
        pass


def _sse_reasoning(text):
    return [
        'data: {"choices":[{"delta":{"reasoning_content":"%s"}}]}' % text,
        "data: [DONE]",
    ]


class ChatStreamReasoningTests(unittest.TestCase):
    """Live deep-thinking must stream per-step, not accumulate across turns.

    Each turn resets the live reasoning so the UI shows only the current step's
    thinking; earlier steps already render as their own persisted blocks.
    """

    def setUp(self) -> None:
        self.run_id = "test_run_reasoning"
        _clear_run_live_text(self.run_id)
        patcher = mock.patch.object(
            chat_stream, "_run_should_stop", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_clear_run_live_text, self.run_id)

    def _run_turn(self, reasoning_text):
        chat_stream.stream_turn_openai_compat(
            self.run_id,
            _FakeResponse(_sse_reasoning(reasoning_text)),
            {},
        )

    def test_single_turn_reasoning_is_live(self) -> None:
        self._run_turn("first thought")
        self.assertEqual(_get_run_live_reasoning(self.run_id), "first thought")

    def test_second_turn_replaces_previous_turn_reasoning(self) -> None:
        self._run_turn("first thought")
        self._run_turn("second thought")
        # Not "first thought\n\nsecond thought" — each step stands alone.
        self.assertEqual(_get_run_live_reasoning(self.run_id), "second thought")


if __name__ == "__main__":
    unittest.main()
