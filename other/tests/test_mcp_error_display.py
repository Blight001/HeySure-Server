import unittest
from types import SimpleNamespace

from api.chat_runtime.chat_prompt_utils import _extract_mcp_error


class McpErrorDisplayTests(unittest.TestCase):
    def test_http_error_uses_response_detail(self):
        response = SimpleNamespace(json=lambda: {"detail": "Session not found"}, text="")
        error = RuntimeError("404 for /internal/mcp/call")
        error.response = response

        self.assertEqual(_extract_mcp_error(error), "Session not found")


if __name__ == "__main__":
    unittest.main()
