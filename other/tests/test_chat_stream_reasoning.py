import unittest

from api.chat_runtime.chat_stream import _merge_run_reasoning


class ChatStreamReasoningTests(unittest.TestCase):
    def test_keeps_previous_turn_reasoning(self) -> None:
        self.assertEqual(
            _merge_run_reasoning("first thought", "second thought"),
            "first thought\n\nsecond thought",
        )

    def test_handles_empty_segments(self) -> None:
        self.assertEqual(_merge_run_reasoning("", "current"), "current")
        self.assertEqual(_merge_run_reasoning("previous", ""), "previous")


if __name__ == "__main__":
    unittest.main()
