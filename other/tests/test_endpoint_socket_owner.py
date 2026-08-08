import unittest

from api.devices.socket_owner import endpoint_dispatch_url, should_reset_endpoint_presence


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

    def test_dispatch_prefers_connector_socket_owner(self) -> None:
        self.assertEqual(
            endpoint_dispatch_url(
                "http://api-gateway:3000/",
                "http://connector-runtime:3002/",
            ),
            "http://connector-runtime:3002",
        )

    def test_dispatch_falls_back_to_gateway_for_legacy_deployments(self) -> None:
        self.assertEqual(
            endpoint_dispatch_url("http://api-gateway:3000/", ""),
            "http://api-gateway:3000",
        )


if __name__ == "__main__":
    unittest.main()
