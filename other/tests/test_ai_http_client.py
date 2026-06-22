import os
import unittest
from unittest.mock import patch

from api.http_client import ai_http_session


class AIHTTPClientTests(unittest.TestCase):
    def test_ignores_environment_proxy_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:7897",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
                "ALL_PROXY": "socks5://127.0.0.1:7897",
            },
        ):
            session = ai_http_session()
            settings = session.merge_environment_settings(
                "https://example.com/v1/chat/completions",
                {},
                None,
                None,
                None,
            )

        self.assertFalse(session.trust_env)
        self.assertEqual(settings["proxies"], {})


if __name__ == "__main__":
    unittest.main()
