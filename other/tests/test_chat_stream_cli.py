import base64
import os
import re
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from api.chat_runtime import chat_stream_cli
from api.chat_runtime.chat_prompt_utils import (
    _clear_run_live_text,
    _get_run_live_reasoning,
)


def _write_fake_cli(tmpdir: str, body: str) -> str:
    """Create a python script standing in for the agent CLI and return a
    cli_command string invoking it."""
    script_path = os.path.join(tmpdir, "fake_cli.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(
            "import json, sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "args = sys.argv[1:]\n"
            + textwrap.dedent(body)
        )
    return f'"{sys.executable}" "{script_path}"'


class StreamTurnCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "test_run_cli"
        self.tmpdir = tempfile.mkdtemp()
        _clear_run_live_text(self.run_id)
        patcher = mock.patch.object(chat_stream_cli, "_run_should_stop", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_clear_run_live_text, self.run_id)

    def _run(self, cli_command: str, convo=None):
        return chat_stream_cli.stream_turn_cli(
            self.run_id,
            "cli://" + cli_command,
            "fake-model",
            convo if convo is not None else [{"role": "user", "content": "你好"}],
            {},
        )

    def test_streams_thought_and_text(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            print(json.dumps({"type": "thought", "data": "thinking"}))
            print(json.dumps({"type": "text", "data": "你好"}))
            print(json.dumps({"type": "text", "data": "世界"}))
            print(json.dumps({"type": "end", "stopReason": "EndTurn", "sessionId": "s1"}))
        """)
        sr = self._run(cmd)
        self.assertEqual(sr.assistant_text, "你好世界")
        self.assertEqual(sr.reasoning_content, "thinking")
        self.assertEqual(sr.finish_reason, "stop")
        self.assertFalse(sr.stopped)
        self.assertIsNone(sr.payload_call)
        self.assertEqual(_get_run_live_reasoning(self.run_id), "thinking")

    def test_prompt_file_carries_system_and_transcript(self) -> None:
        # The fake CLI reads back the --prompt-file it was given and echoes it
        # as text, letting us assert the convo serialization end to end.
        cmd = _write_fake_cli(self.tmpdir, """
            path = args[args.index("--prompt-file") + 1]
            content = open(path, encoding="utf-8").read()
            print(json.dumps({"type": "text", "data": content}))
            print(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        """)
        convo = [
            {"role": "system", "content": "你是助手小七"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": [{"type": "text", "text": "第二问"}]},
        ]
        sr = self._run(cmd, convo=convo)
        self.assertIn("[系统设定]\n你是助手小七", sr.assistant_text)
        self.assertIn("User: 第一问", sr.assistant_text)
        self.assertIn("Assistant: 第一答", sr.assistant_text)
        self.assertIn("User: 第二问", sr.assistant_text)

    def test_image_block_is_materialized_for_cli_vision_and_cleaned(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            path = args[args.index("--prompt-file") + 1]
            content = open(path, encoding="utf-8").read()
            print(json.dumps({"type": "text", "data": content}))
            print(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        """)
        image_data = b"not-a-real-png-but-valid-transport-bytes"
        data_url = "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")
        convo = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请查看截图"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]

        sr = self._run(cmd, convo=convo)

        self.assertIn("请查看截图", sr.assistant_text)
        self.assertIn("[图片附件]", sr.assistant_text)
        self.assertIn("必须使用 read_file", sr.assistant_text)
        self.assertNotIn("CLI 模型不支持图片输入", sr.assistant_text)
        match = re.search(r"图片绝对路径：(.+)", sr.assistant_text)
        self.assertIsNotNone(match)
        self.assertFalse(os.path.exists(match.group(1).strip()))

    def test_cli_enables_read_file_for_materialized_images(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            tools = args[args.index("--tools") + 1]
            print(json.dumps({"type": "text", "data": tools}))
            print(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        """)
        sr = self._run(cmd)
        self.assertIn("read_file", sr.assistant_text.split(","))

    def test_mcp_text_protocol_truncates_and_kills(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            print(json.dumps({"type": "text", "data": "前置说明 CALL_MARKER"}))
            print(json.dumps({"type": "text", "data": " 之后的内容不应保留"}))
            print(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        """)

        class _Match:
            def end(self):
                return len("前置说明 CALL_MARKER")

        def fake_extract(text):
            if "CALL_MARKER" in text:
                return {"tool": "demo.tool", "arguments": {}}, _Match()
            return None, None

        with mock.patch.object(
            chat_stream_cli, "_extract_first_complete_mcp_call", side_effect=fake_extract
        ):
            sr = self._run(cmd)
        self.assertEqual(sr.assistant_text, "前置说明 CALL_MARKER")
        self.assertEqual(sr.payload_call, {"tool": "demo.tool", "arguments": {}})
        self.assertEqual(sr.finish_reason, "mcp_wait")

    def test_nonzero_exit_without_output_raises(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            print("boom failure", file=sys.stderr)
            sys.exit(3)
        """)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(cmd)
        self.assertIn("退出码 3", str(ctx.exception))
        self.assertIn("boom failure", str(ctx.exception))

    def test_missing_command_raises_friendly_error(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._run("definitely_not_a_real_cli_command_12345")
        self.assertIn("CLI 命令未找到", str(ctx.exception))

    def test_stop_request_kills_and_marks_stopped(self) -> None:
        cmd = _write_fake_cli(self.tmpdir, """
            import time
            print(json.dumps({"type": "text", "data": "开始"}), flush=True)
            time.sleep(30)
            print(json.dumps({"type": "end", "stopReason": "EndTurn"}))
        """)
        with mock.patch.object(chat_stream_cli, "_run_should_stop", return_value=True):
            sr = self._run(cmd)
        self.assertTrue(sr.stopped)


if __name__ == "__main__":
    unittest.main()
