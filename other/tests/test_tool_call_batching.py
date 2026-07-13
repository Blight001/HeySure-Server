"""Every tool call a model emits in one turn must survive to the worker.

The worker used to keep only the first call of a turn and discard the rest,
forcing one tool per round trip on every provider. These tests pin the batch
behaviour end to end: the parser finds all text-protocol blocks, both streaming
paths surface all native tool calls, and the assembled prompt no longer tells
the model to call tools one at a time.
"""

import json
import unittest
from unittest import mock

from api.chat_runtime import chat_stream
from api.chat_runtime.chat_prompt_utils import (
    _clear_run_live_text,
    _strip_stale_serial_call_rules,
)
from api.chat_runtime.mcp_parser import extract_all_complete_mcp_calls
from api.chat_runtime.run_state import _AUTO_RUNTIME_SECTION_TITLES
from api.models import DEFAULT_MCP_FORMAT_ERROR_HINT
from api.models.defaults import MCP_BATCH_CALL_RULE
from ai_runtime.inference.core import _append_missing_tool_responses


class _FakeResponse:
    """Minimal stand-in for a streaming ``requests.Response``."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8")

    def close(self):
        pass


def _sse(*chunks):
    return [f"data: {json.dumps(chunk)}" for chunk in chunks] + ["data: [DONE]"]


def _native_tc(index, call_id, name, arguments):
    return {
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": index,
                    "id": call_id,
                    "function": {"name": name, "arguments": arguments},
                }]
            }
        }]
    }


class TextProtocolBatchTests(unittest.TestCase):
    """Models without native function calling (grok CLI, deepseek-reasoner)."""

    def test_extracts_every_mcp_call_block(self) -> None:
        text = (
            "先看两个文件。\n"
            '<mcp-call>{"tool":"workspace.run+command","arguments":{"command":"dir"}}</mcp-call>\n'
            '<mcp-call>{"tool":"knowledge.search","arguments":{"query":"部署"}}</mcp-call>\n'
        )
        calls = extract_all_complete_mcp_calls(text)
        self.assertEqual(
            [payload["tool"] for payload, _ in calls],
            ["workspace.run+command", "knowledge.search"],
        )
        self.assertEqual(calls[1][0]["arguments"], {"query": "部署"})

    def test_single_call_still_parses(self) -> None:
        text = '<mcp-call>{"tool":"knowledge.search","arguments":{"query":"x"}}</mcp-call>'
        calls = extract_all_complete_mcp_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["tool"], "knowledge.search")

    def test_no_call_returns_empty(self) -> None:
        self.assertEqual(extract_all_complete_mcp_calls("just talking"), [])

    def test_overlapping_syntaxes_are_not_double_counted(self) -> None:
        # An <mcp-call> wrapping a <tool_call> body matches two regexes; the
        # inner one must not surface as a second, duplicate call.
        text = (
            "<mcp-call>\n"
            '<tool_call>{"tool":"knowledge.search","arguments":{"query":"x"}}</tool_call>\n'
            "</mcp-call>"
        )
        calls = extract_all_complete_mcp_calls(text)
        self.assertEqual(len(calls), 1)


class OpenAICompatBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "test_run_batch_oa"
        _clear_run_live_text(self.run_id)
        patcher = mock.patch.object(chat_stream, "_run_should_stop", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_clear_run_live_text, self.run_id)

    def _stream(self, lines, name_map=None):
        return chat_stream.stream_turn_openai_compat(
            self.run_id, _FakeResponse(lines), name_map or {}
        )

    def test_parallel_native_tool_calls_all_survive(self) -> None:
        sr = self._stream(
            _sse(
                _native_tc(0, "call_a", "workspace_run-command", '{"command":'),
                _native_tc(0, "", "", '"dir"}'),
                _native_tc(1, "call_b", "knowledge_search", '{"query":"x"}'),
            ),
            {"workspace_run-command": "workspace.run+command", "knowledge_search": "knowledge.search"},
        )
        self.assertTrue(sr.has_native_tc)
        self.assertEqual(
            [c["tool"] for c in sr.tool_calls],
            ["workspace.run+command", "knowledge.search"],
        )
        # Fragmented argument deltas are reassembled per call index.
        self.assertEqual(sr.tool_calls[0]["arguments"], {"command": "dir"})
        self.assertEqual(sr.tool_calls[0]["id"], "call_a")
        self.assertEqual(sr.tool_calls[1]["id"], "call_b")
        # The legacy singular fields still mirror the first call.
        self.assertEqual(sr.payload_call, {"tool": "workspace.run+command", "arguments": {"command": "dir"}})

    def test_text_protocol_batch_over_sse(self) -> None:
        sr = self._stream(_sse(
            {"choices": [{"delta": {"content": '<mcp-call>{"tool":"a.b","arguments":{}}</mcp-call>'}}]},
            {"choices": [{"delta": {"content": '<mcp-call>{"tool":"c.d","arguments":{}}</mcp-call>'}}]},
        ))
        self.assertFalse(sr.has_native_tc)
        self.assertEqual([c["tool"] for c in sr.tool_calls], ["a.b", "c.d"])
        self.assertEqual(sr.finish_reason, "mcp_wait")

    def test_trailing_chatter_after_last_call_is_trimmed(self) -> None:
        sr = self._stream(_sse(
            {"choices": [{"delta": {"content": '<mcp-call>{"tool":"a.b","arguments":{}}</mcp-call>'}}]},
            {"choices": [{"delta": {"content": "\n我等下再总结。"}}]},
        ))
        self.assertEqual([c["tool"] for c in sr.tool_calls], ["a.b"])
        self.assertNotIn("我等下再总结", sr.assistant_text)

    def test_plain_answer_yields_no_calls(self) -> None:
        sr = self._stream(_sse({"choices": [{"delta": {"content": "好的，完成了。"}}]}))
        self.assertEqual(sr.tool_calls, [])
        self.assertIsNone(sr.payload_call)
        self.assertEqual(sr.assistant_text, "好的，完成了。")


class AnthropicBatchTests(unittest.TestCase):
    def test_every_tool_use_block_survives(self) -> None:
        sr = chat_stream.StreamResult()
        sr.has_native_tc = True
        blocks = [
            {"id": "toolu_1", "name": "workspace_run-command", "arguments": '{"command":"dir"}'},
            {"id": "toolu_2", "name": "knowledge_search", "arguments": '{"query":"x"}'},
        ]
        chat_stream._finalize_native_tool_calls(
            sr, blocks, {"workspace_run-command": "workspace.run+command"}
        )
        self.assertEqual(
            [c["tool"] for c in sr.tool_calls],
            # Unmapped names pass through unchanged rather than being dropped.
            ["workspace.run+command", "knowledge_search"],
        )
        self.assertEqual(sr.tc_id, "toolu_1")

    def test_malformed_arguments_degrade_to_empty_dict(self) -> None:
        sr = chat_stream.StreamResult()
        sr.has_native_tc = True
        chat_stream._finalize_native_tool_calls(
            sr, [{"id": "t1", "name": "a_b", "arguments": "{not json"}], {}
        )
        self.assertEqual(sr.tool_calls[0]["arguments"], {})


class ConvoShapeTests(unittest.TestCase):
    """An assistant's tool_calls and their tool responses must stay contiguous.

    Screenshot results ride in a *user* message. Appending one straight after the
    capturing tool's response would wedge it between two tool messages, orphaning
    every later tool_call_id — so the worker holds images back until the batch
    drains. ``_append_missing_tool_responses`` is the runtime backstop for that
    invariant, and these tests pin both sides of it.
    """

    def test_batched_convo_with_trailing_image_needs_no_repair(self) -> None:
        convo = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "cap", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "srch", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
            {"role": "user", "content": [{"type": "text", "text": "screenshot"}]},
        ]
        before = len(convo)
        self.assertEqual(_append_missing_tool_responses(convo, "err"), [])
        self.assertEqual(len(convo), before)  # nothing dropped, nothing synthesized

    def test_image_between_tool_responses_orphans_the_later_call(self) -> None:
        # The shape the worker must never emit: the image splits the responses,
        # so tool "b" is orphaned — the repair drops it and fabricates a failure.
        convo = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "cap", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "srch", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "user", "content": "screenshot"},
            {"role": "tool", "tool_call_id": "b", "content": "{\"real\": true}"},
        ]
        self.assertEqual(_append_missing_tool_responses(convo, "err"), ["b"])
        # b's real result is gone, replaced by a synthetic failure.
        recovered = [m for m in convo if m.get("role") == "tool" and m.get("tool_call_id") == "b"]
        self.assertIn("recovered", recovered[0]["content"])


class PromptBatchRuleTests(unittest.TestCase):
    """A prompt persisted before batching must not re-teach serial calling.

    The rule is injected per-run by ``build_runtime_system_prompt_and_tools``
    (the path the worker and the /system-prompt-preview endpoint share), not
    written into persona files — existing personas are never rewritten, yet they
    still have to learn that a turn may carry several calls.
    """

    def test_stale_serial_rule_is_stripped(self) -> None:
        stale = "注意：\n- 一次只调用一个工具，等待 MCP 返回后再继续。\n- 别的规则"
        cleaned = _strip_stale_serial_call_rules(stale)
        self.assertNotIn("一次只调用一个工具", cleaned)
        self.assertIn("别的规则", cleaned)

    def test_english_stale_rule_is_stripped(self) -> None:
        stale = "Rules:\n- Call exactly one tool per <mcp-call> block; never join two tool names into one name.\n- keep"
        cleaned = _strip_stale_serial_call_rules(stale)
        self.assertNotIn("Call exactly one tool per", cleaned)
        self.assertIn("keep", cleaned)

    def test_batch_rule_is_a_registered_runtime_section(self) -> None:
        # Registered so a persona that ever persists the section is healed on
        # load instead of ending up with two copies.
        self.assertIn("MCP 批量调用", _AUTO_RUNTIME_SECTION_TITLES)
        self.assertIn("相互独立、彼此不依赖的工具", MCP_BATCH_CALL_RULE)

    def test_format_error_hint_no_longer_teaches_serial_calling(self) -> None:
        # The shipped hint fires on a malformed call; if it still said "one tool
        # at a time" it would undo the batching right when the model is listening.
        self.assertNotIn("一次只调用一个工具", DEFAULT_MCP_FORMAT_ERROR_HINT)


if __name__ == "__main__":
    unittest.main()
