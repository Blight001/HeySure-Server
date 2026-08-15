"""MCP schemas for the external-controller conversation queue."""


def conversation_tool_definitions() -> list[dict]:
    return [
        {
            "name": "heysure.list_messages",
            "description": "List queued or in-progress user messages waiting for this external controller.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.claim_message",
            "description": "Atomically claim the oldest queued user message and return its conversation history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "turn_id": {"type": "string"},
                    "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 1800},
                    "history_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.renew_message",
            "description": "Extend the lease for a claimed message while a long Codex turn is still running.",
            "inputSchema": {
                "type": "object",
                "required": ["turn_id"],
                "properties": {
                    "turn_id": {"type": "string"},
                    "lease_seconds": {"type": "integer", "minimum": 30, "maximum": 1800},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.reply_message",
            "description": "Persist an assistant reply for a claimed message and complete it exactly once.",
            "inputSchema": {
                "type": "object",
                "required": ["turn_id", "content"],
                "properties": {
                    "turn_id": {"type": "string"},
                    "content": {"type": "string"},
                    "think": {"type": "string"},
                    "model": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "heysure.fail_message",
            "description": "Move a claimed message to a terminal failed state with a recoverable error explanation.",
            "inputSchema": {
                "type": "object",
                "required": ["turn_id", "error"],
                "properties": {"turn_id": {"type": "string"}, "error": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    ]
