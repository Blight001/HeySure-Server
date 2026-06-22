import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mcp_runtime.mcp import registry
from tools.conversation import _edit_conversation


class ConversationEditDefaultsTests(unittest.TestCase):
    def test_manage_schema_exposes_clear_action(self):
        tool = next(item for item in registry.list_tools() if item["name"] == "conversation.manage")

        # The unified tool requires an explicit action; clear/rename/compress are
        # all reachable through it.
        self.assertEqual(tool["inputSchema"]["required"], ["action"])
        self.assertIn("clear", tool["inputSchema"]["properties"]["action"]["enum"])
        self.assertIn("rename", tool["inputSchema"]["properties"]["action"]["enum"])

    def test_empty_arguments_clear_current_conversation(self):
        session_row = SimpleNamespace(updated_at=0)
        query_result = MagicMock()
        query_result.first.return_value = session_row
        message_result = MagicMock()
        message_result.all.return_value = []

        db = MagicMock()
        db.exec.side_effect = [query_result, message_result]
        session_manager = MagicMock()
        session_manager.__enter__.return_value = db
        session_manager.__exit__.return_value = False

        scope = {
            "session_id": "current-session",
            "ai_kind": "core",
            "ai_config_id": 34,
            "current_message_id": None,
        }
        with (
            patch("tools.conversation._conversation_scope", return_value=scope),
            patch("tools.conversation.Session", return_value=session_manager),
            patch("tools.conversation.delete_message_media"),
            patch("tools.conversation._rebuild_usage_snapshots"),
        ):
            result = _edit_conversation(user_id=1, args={}, ai_config_id=34)

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "clear")
        self.assertEqual(result["session_id"], "current-session")

    def test_clear_ignores_model_supplied_scope_when_run_context_exists(self):
        supplied = {
            "action": "clear",
            "session_id": "",
            "ai_config_id": 1,
            "ai_kind": "beta",
            "keep_current_message": True,
        }
        resolved_scope = {
            "session_id": "real-session",
            "ai_kind": "core",
            "ai_config_id": 34,
            "current_message_id": None,
        }

        with (
            patch(
                "tools.conversation.get_run_session_context",
                return_value={"session_id": "real-session", "ai_kind": "core", "ai_config_id": 34},
            ),
            patch(
                "tools.conversation._conversation_scope",
                return_value=resolved_scope,
            ) as resolve_scope,
            patch("tools.conversation.Session") as session_type,
        ):
            session_type.return_value.__enter__.return_value.exec.return_value.first.return_value = None
            with self.assertRaisesRegex(Exception, "Session not found"):
                _edit_conversation(user_id=1, args=supplied, ai_config_id=34)

        scope_args = resolve_scope.call_args.args[0]
        self.assertNotIn("session_id", scope_args)
        self.assertNotIn("ai_config_id", scope_args)
        self.assertNotIn("ai_kind", scope_args)
        self.assertTrue(scope_args["keep_current_message"])


if __name__ == "__main__":
    unittest.main()
