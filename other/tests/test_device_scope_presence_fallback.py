import unittest
from unittest.mock import patch

from gateway.routers import devices


class DeviceScopePresenceFallbackTests(unittest.TestCase):
    def test_connector_presence_is_used_when_gateway_has_no_socket(self) -> None:
        snapshot = {
            "id": "linux-1",
            "userId": 1,
            "deviceType": "custom",
            "online": True,
        }
        with (
            patch.object(devices, "agents", {}),
            patch.object(devices, "online_agent_snapshot_for_user", return_value=snapshot) as lookup,
        ):
            self.assertIs(devices._find_connected_agent("linux-1", 1), snapshot)
            lookup.assert_called_once_with(1, "linux-1")

    def test_local_socket_wins_without_database_lookup(self) -> None:
        local = {"id": "linux-1", "userId": 1, "deviceType": "custom"}
        with (
            patch.object(devices, "agents", {"sid": local}),
            patch.object(devices, "online_agent_snapshot_for_user") as lookup,
        ):
            self.assertIs(devices._find_connected_agent("linux-1", 1), local)
            lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
