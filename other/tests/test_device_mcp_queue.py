import unittest
from unittest.mock import AsyncMock, patch

from connector_runtime.dispatch import device_dispatch


class DeviceMcpQueueTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        device_dispatch._PENDING_DISPATCHES.clear()
        device_dispatch._PENDING_DISPATCH_WAITERS.clear()

    async def _dispatch(self):
        return await device_dispatch.dispatch_task_to_agent(
            device_id="desktop-1",
            user_id=7,
            ai_config_id=11,
            ai_kind="core",
            session_id="session-1",
            session_name="test",
            model=None,
            instruction="Run endpoint MCP tool mouse.click",
            tool="mouse.click",
            args={"x": 10, "y": 20},
            allowed_tools=["mouse.click"],
            wait_for_result=False,
            suppress_session_message=True,
        )

    async def test_idle_device_dispatches_immediately(self):
        emit = AsyncMock()
        with (
            patch.object(device_dispatch, "_find_agent_sid", return_value="sid-1"),
            patch.object(device_dispatch, "_enqueue_dispatch_row", return_value="pending"),
            patch.object(device_dispatch.sio, "emit", emit),
        ):
            result = await self._dispatch()

        self.assertEqual(result["status"], "pending")
        emit.assert_awaited_once()

    async def test_busy_device_queues_without_second_emit(self):
        emit = AsyncMock()
        resume = AsyncMock(return_value=None)
        with (
            patch.object(device_dispatch, "_find_agent_sid", return_value="sid-1"),
            patch.object(device_dispatch, "_enqueue_dispatch_row", return_value="queued"),
            patch.object(device_dispatch, "resume_device_dispatch_queue", resume),
            patch.object(device_dispatch.sio, "emit", emit),
        ):
            result = await self._dispatch()

        self.assertEqual(result["status"], "queued")
        emit.assert_not_awaited()
        resume.assert_awaited_once_with("desktop-1")


if __name__ == "__main__":
    unittest.main()
