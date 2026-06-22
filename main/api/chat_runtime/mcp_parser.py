"""Pure text parsing helpers for model-emitted MCP calls."""

import json
import re
from typing import Any, Dict, Optional, Tuple


MCP_CALL_BLOCK_RE = re.compile(
    r"<mcp[-_]call>\s*([\s\S]*?)\s*</\s*(?:mcp[-_]call|[\uFF5C|]*\s*DSML\s*[\uFF5C|]*\s*(?:invoke|tool[-_]?calls?))\s*>",
    re.IGNORECASE,
)


def parse_mcp_payload(raw: str) -> Optional[Dict[str, Any]]:
    body = (raw or "").strip()
    if not body:
        return None

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        tool = str(payload.get("tool", "")).strip()
        if not tool:
            return None
        args = payload.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        return {"tool": tool, "arguments": args}
    except Exception:
        pass

    tool_match = re.search(r"<tool>\s*([\s\S]*?)\s*</tool>", body, re.IGNORECASE)
    if not tool_match:
        return None
    tool = str(tool_match.group(1) or "").strip()
    if not tool:
        return None

    args_match = re.search(r"<arguments>\s*([\s\S]*?)\s*</arguments>", body, re.IGNORECASE)
    if not args_match:
        return {"tool": tool, "arguments": {}}
    args_raw = str(args_match.group(1) or "").strip()
    if not args_raw:
        return {"tool": tool, "arguments": {}}
    try:
        args = json.loads(args_raw)
        if isinstance(args, dict):
            return {"tool": tool, "arguments": args}
        return {"tool": tool, "arguments": {}}
    except Exception:
        return None


def extract_first_complete_mcp_call(assistant_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[re.Match]]:
    match = MCP_CALL_BLOCK_RE.search(assistant_text or "")
    if match:
        payload = parse_mcp_payload(match.group(1))
        if payload:
            return payload, match

    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    for fence_match in fence_pattern.finditer(assistant_text or ""):
        payload = parse_mcp_payload(fence_match.group(1))
        if payload:
            return payload, fence_match

    return None, None


def extract_first_mcp_call(assistant_text: str) -> Optional[Dict[str, Any]]:
    payload, _ = extract_first_complete_mcp_call(assistant_text)
    return payload

