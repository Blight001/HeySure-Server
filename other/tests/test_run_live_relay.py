import unittest
from unittest.mock import AsyncMock, patch

from api.chat_runtime.run_state import (
    _RUN_LIVE_META,
    _RUN_LIVE_STATE,
    _RUN_STATE_LOCK,
    apply_relayed_run_live_state,
)
from gateway.routers.socket_relay import EmitRequest, relay_emit


class RunLiveRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        with _RUN_STATE_LOCK:
            _RUN_LIVE_STATE.clear()
            _RUN_LIVE_META.clear()

    def tearDown(self) -> None:
        with _RUN_STATE_LOCK:
            _RUN_LIVE_STATE.clear()
            _RUN_LIVE_META.clear()

    def test_mirrors_worker_payload_to_gateway_state(self) -> None:
        applied = apply_relayed_run_live_state({
            "run_id": "run_123",
            "user_id": 7,
            "text": "hello",
            "reasoning": "thinking",
            "phase": "waiting_mcp",
            "current_tool": "workspace.run_command",
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "updated_at": 123.5,
        })

        self.assertTrue(applied)
        self.assertEqual(_RUN_LIVE_STATE["run_123"]["text"], "hello")
        self.assertEqual(_RUN_LIVE_STATE["run_123"]["reasoning"], "thinking")
        self.assertEqual(
            _RUN_LIVE_STATE["run_123"]["pending_total_tokens"],
            14,
        )
        self.assertEqual(_RUN_LIVE_META["run_123"]["user_id"], 7)

    def test_ignores_out_of_order_snapshot(self) -> None:
        apply_relayed_run_live_state({
            "run_id": "run_123",
            "text": "new",
            "updated_at": 20,
        })

        applied = apply_relayed_run_live_state({
            "run_id": "run_123",
            "text": "old",
            "updated_at": 10,
        })

        self.assertFalse(applied)
        self.assertEqual(_RUN_LIVE_STATE["run_123"]["text"], "new")

    def test_rejects_payload_without_run_id(self) -> None:
        self.assertFalse(apply_relayed_run_live_state({"text": "orphan"}))
        self.assertEqual(_RUN_LIVE_STATE, {})

    async def test_socket_relay_mirrors_and_broadcasts_live_event(
        self,
    ) -> None:
        request = EmitRequest(
            event="chat:run_live",
            data={"run_id": "run_456", "text": "streamed", "updated_at": 30},
            room="user_7",
        )

        with patch(
            "gateway.routers.socket_relay.sio.emit",
            new=AsyncMock(),
        ) as emit:
            result = await relay_emit(request)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(_RUN_LIVE_STATE["run_456"]["text"], "streamed")
        emit.assert_awaited_once_with(
            "chat:run_live",
            request.data,
            room="user_7",
        )


if __name__ == "__main__":
    unittest.main()
