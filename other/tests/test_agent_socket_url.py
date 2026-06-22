import unittest
from unittest.mock import patch

from starlette.requests import Request

from gateway.routers.auth import _agent_socket_url


def make_request(*, host: str, forwarded_host: str = "", forwarded_proto: str = "") -> Request:
    headers = [(b"host", host.encode())]
    if forwarded_host:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    return Request({
        "type": "http",
        "scheme": "http",
        "server": ("api-gateway", 3000),
        "path": "/api/auth/agent-endpoint",
        "query_string": b"",
        "headers": headers,
    })


class AgentSocketUrlTests(unittest.TestCase):
    @patch("gateway.routers.auth.settings.agent_socket_url", "")
    @patch("gateway.routers.auth.settings.public_base_url", "")
    def test_prefers_forwarded_public_host_over_internal_proxy_host(self) -> None:
        request = make_request(
            host="api-gateway:3000",
            forwarded_host="console.example.com",
            forwarded_proto="https",
        )

        self.assertEqual(_agent_socket_url(request), "https://console.example.com")

    @patch("gateway.routers.auth.settings.agent_socket_url", "")
    @patch("gateway.routers.auth.settings.public_base_url", "")
    def test_uses_request_host_without_a_proxy(self) -> None:
        request = make_request(host="127.0.0.1:3000")

        self.assertEqual(_agent_socket_url(request), "http://127.0.0.1:3000")


if __name__ == "__main__":
    unittest.main()
