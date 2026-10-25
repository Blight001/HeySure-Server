"""Tests for the protocol-compatible MCP program orchestrator."""

import asyncio
import importlib
import time
import unittest
from unittest.mock import patch

from tools import program


class _FakeRegistry:
    def __init__(self):
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    def has(self, name):
        return name in {"knowledge.search", "workspace.file+manage", "slow.one", "slow.two"}

    async def call(self, name, user_id, arguments, ai_config_id):
        self.calls.append((name, user_id, arguments, ai_config_id))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if name == "knowledge.search":
                return {
                    "tool": name,
                    "result": {"items": [{"file_ref": "file_a"}, {"file_ref": "file_b"}]},
                }
            if name == "workspace.file+manage":
                return {"tool": name, "result": {"file_ref": arguments["file_ref"], "ok": True}}
            await asyncio.sleep(0.05)
            return {"tool": name, "result": arguments}
        finally:
            self.in_flight -= 1


class McpProgramTests(unittest.TestCase):
    def test_validate_rejects_dependency_cycle_before_execution(self):
        with self.assertRaises(program.ProgramValidationError):
            program.validate_program({
                "steps": [
                    {"id": "a", "tool": "knowledge.search", "depends_on": ["b"]},
                    {"id": "b", "tool": "knowledge.search", "depends_on": ["a"]},
                ]
            })

    def test_render_preserves_exact_value_types(self):
        context = {"input": {"count": 3, "items": ["a"]}}
        self.assertEqual(program.render_value("${input.count}", context), 3)
        self.assertEqual(program.render_value("count=${input.count}", context), "count=3")
        self.assertEqual(program.render_value("${input.items}", context), ["a"])

    def test_dependency_and_foreach_execution(self):
        fake = _FakeRegistry()
        definition = {
            "steps": [
                {
                    "id": "search",
                    "tool": "knowledge.search",
                    "arguments": {"query": "${input.query}"},
                },
                {
                    "id": "details",
                    "tool": "workspace.file+manage",
                    "depends_on": ["search"],
                    "foreach": "${steps.search.result.items}",
                    "arguments": {"action": "info", "file_ref": "${item.file_ref}"},
                    "concurrency": 2,
                },
            ],
            "output": {
                "search_count": "${steps.search.result.items}",
                "details": "${steps.details.items}",
            },
        }
        registry_module = importlib.import_module("mcp_runtime.mcp.registry")
        with patch.object(registry_module, "registry", fake):
            result = asyncio.run(
                program.execute_program(definition, user_id=7, ai_config_id=None, input_value={"query": "x"})
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(result["output"]["search_count"][0]["file_ref"], "file_a")
        self.assertTrue(all(item["ok"] for item in result["output"]["details"]))

    def test_independent_steps_run_concurrently(self):
        fake = _FakeRegistry()
        definition = {
            "steps": [
                {"id": "a", "tool": "slow.one", "arguments": {}},
                {"id": "b", "tool": "slow.two", "arguments": {}},
            ]
        }
        registry_module = importlib.import_module("mcp_runtime.mcp.registry")
        with patch.object(registry_module, "registry", fake):
            started = time.monotonic()
            result = asyncio.run(program.execute_program(definition, user_id=7, ai_config_id=None))
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "completed")
        self.assertEqual(fake.max_in_flight, 2)
        self.assertLess(elapsed, 0.09)

    def test_nested_orchestrator_is_rejected(self):
        with self.assertRaises(program.ProgramValidationError):
            program.validate_program({
                "steps": [{"id": "nested", "tool": "mcp.program", "arguments": {}}]
            })

    def test_describe_meta_tool_bypasses_toolbox_binding_gate(self):
        from mcp_runtime.mcp import core

        with patch("tools.engine.enforce_toolbox_binding", side_effect=AssertionError):
            core._enforce_workshop_binding("mcp.describe+tool", 7, 9)

    def test_program_uses_toolbox_binding_gate(self):
        from mcp_runtime.mcp import core

        with patch("tools.engine.enforce_toolbox_binding") as gate:
            core._enforce_workshop_binding("mcp.program", 7, 9)
        gate.assert_called_once_with("mcp.program", 7, 9)
