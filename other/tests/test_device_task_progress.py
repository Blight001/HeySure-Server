import unittest
from unittest.mock import AsyncMock, patch

from connector_runtime.dispatch.device_dispatch import handle_task_progress


class DeviceTaskProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_event_carries_chat_routing_context(self) -> None:
        context = {
            "device_id": "desktop_1",
            "user_id": 7,
            "ai_config_id": 12,
            "ai_kind": "assistant",
            "session_id": "session_3",
            "tool": "screen.capture",
        }

        with (
            patch(
                "connector_runtime.dispatch.device_dispatch._resolve_result_context",
                return_value=context,
            ),
            patch(
                "connector_runtime.dispatch.device_dispatch._emit_to_user",
                new=AsyncMock(),
            ) as emit,
        ):
            await handle_task_progress({
                "taskId": "task_9",
                "progress": 0,
                "message": "开始执行",
            })

        event, payload = emit.await_args.args[1:]
        self.assertEqual(event, "device:task_progress")
        self.assertEqual(payload["sessionId"], "session_3")
        self.assertEqual(payload["aiConfigId"], 12)
        self.assertEqual(payload["aiKind"], "assistant")
        self.assertEqual(payload["tool"], "screen.capture")
        self.assertEqual(payload["progress"], 0)


if __name__ == "__main__":
    unittest.main()
