import unittest

from api.devices.socket_owner import should_reset_endpoint_presence


class EndpointSocketOwnerTests(unittest.TestCase):
    def test_connector_always_resets_presence(self) -> None:
        self.assertTrue(
            should_reset_endpoint_presence("connector", "http://connector-runtime:3002")
        )

    def test_split_gateway_preserves_connector_presence(self) -> None:
        self.assertFalse(
            should_reset_endpoint_presence("gateway", "http://connector-runtime:3002")
        )

    def test_monolith_gateway_resets_presence(self) -> None:
        self.assertTrue(should_reset_endpoint_presence("gateway", ""))
        self.assertTrue(should_reset_endpoint_presence("gateway", "   "))

    def test_non_socket_runtime_never_resets_presence(self) -> None:
        self.assertFalse(should_reset_endpoint_presence("worker", ""))
        self.assertFalse(should_reset_endpoint_presence("mcp", ""))


if __name__ == "__main__":
    unittest.main()
