import asyncio
import unittest
from unittest.mock import MagicMock

from api.sio import _RemoteSio


class RemoteSioTests(unittest.TestCase):
    def test_reuses_client_across_short_lived_event_loops(self) -> None:
        relay = _RemoteSio("http://gateway.test")
        client = MagicMock()
        response = MagicMock()
        client.post.return_value = response
        relay._client = client

        asyncio.run(relay.emit("chat:run_live", {"text": "first"}))
        asyncio.run(relay.emit("chat:run_live", {"text": "second"}))

        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(response.raise_for_status.call_count, 2)


if __name__ == "__main__":
    unittest.main()
