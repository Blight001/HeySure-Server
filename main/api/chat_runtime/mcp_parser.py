"""Pure text parsing helpers for model-emitted MCP calls.

Models are instructed to emit tool calls in the canonical form::

    <mcp-call>{"tool":"name","arguments":{...}}</mcp-call>

In practice they frequently fall back to whatever tool-calling syntax they were
pre-trained on. To stay robust we additionally recognise the common variants and
normalise every one of them to ``{"tool": str, "arguments": dict}``:

  * Anthropic / DSML style XML (with optional namespace prefixes such as
    ``mcp:`` / ``antml:`` / ``｜DSML｜`` and an optional
    ``<tool_calls>`` / ``<function_calls>`` wrapper)::

        <invoke name="tool"><parameter name="x">v</parameter></invoke>

  * Hermes / Qwen style::

        <tool_call>{"name":"tool","arguments":{...}}</tool_call>

  * Grok / xAI style::

        <xai:function_call name="tool"><parameter name="x">v</parameter></xai:function_call>

  * Bare JSON inside a ``` fenced ``` block.

The JSON payloads also accept aliased keys (``name`` for ``tool``;
``parameters`` / ``params`` / ``input`` / ``args`` for ``arguments``).
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple


MCP_CALL_BLOCK_RE = re.compile(
    r"<mcp[-_]call>\s*([\s\S]*?)\s*</\s*(?:mcp[-_]call|[｜|]*\s*DSML\s*[｜|]*\s*(?:invoke|tool[-_]?calls?))\s*>",
    re.IGNORECASE,
)

# Anthropic / DSML style ``<...invoke name="tool"> ... </...invoke>``. Tolerates
# namespace prefixes (``mcp:`` / ``antml:`` / ``｜DSML｜`` / ``function-``) on both
# the opening and closing tag, and quoted or bare ``name`` attribute values.
INVOKE_BLOCK_RE = re.compile(
    r"<[^<>]*?\binvoke\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>"
    r"([\s\S]*?)"
    r"</[^<>]*?\binvoke\b[^<>]*?>",
    re.IGNORECASE,
)

# Individual ``<parameter name="x">value</parameter>`` tags inside an invoke block.
INVOKE_PARAM_RE = re.compile(
    r"<[^<>]*?\bparameter\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>"
    r"([\s\S]*?)"
    r"</[^<>]*?\bparameter\b[^<>]*?>",
    re.IGNORECASE,
)

# Hermes / Qwen style ``<tool_call> {json} </tool_call>``. ``\bcall\b`` keeps the
# plural ``<tool_calls>`` wrapper from matching here.
TOOL_CALL_JSON_RE = re.compile(
    r"<[^<>]*?\btool[_-]?call\b[^<>]*?>\s*([\s\S]*?)\s*</[^<>]*?\btool[_-]?call\b[^<>]*?>",
    re.IGNORECASE,
)

# Grok / xAI style ``<xai:function_call name="tool"> <parameter name="x">v</parameter>
# </xai:function_call>``. Requiring a ``name`` attribute on the opening tag (and the
# singular ``call``) keeps the attribute-less plural ``<function_calls>`` wrapper in
# _WRAPPER_TAG_RE territory instead of this branch.
FUNCTION_CALL_BLOCK_RE = re.compile(
    r"<[^<>]*?\bfunction[_-]?call\b[^<>]*?\bname\s*=\s*[\"']?([^\"'>\s]+)[\"']?[^<>]*?>"
    r"([\s\S]*?)"
    r"</[^<>]*?\bfunction[_-]?call\b[^<>]*?>",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)

# Bare wrapper / namespace tags left behind once a block is stripped, e.g.
# ``<mcp:tool_calls>`` … ``</function_calls>``.
_WRAPPER_TAG_RE = re.compile(
    r"</?[^<>]*?\b(?:tool[_-]?calls?|function[_-]?calls?)\b[^<>]*?>",
    re.IGNORECASE,
)

# A tool-call block that started streaming but never closed — strip from the
# opening tag to end of text so partial tool syntax never reaches the user.
_PARTIAL_TAIL_RE = re.compile(
    r"<[^<>]*?\b(?:mcp[-_]call|invoke|parameter|tool[_-]?calls?|function[_-]?calls?)\b[\s\S]*$",
    re.IGNORECASE,
)


_TOOL_KEYS = ("tool", "name", "tool_name", "toolName")
_ARGS_KEYS = ("arguments", "parameters", "params", "input", "args")


def _normalize_tool_name(name: str) -> str:
    tool = str(name or "").strip()
    # Some providers wrap the callable as ``functions.<tool>``; no real tool
    # namespace is ``functions`` so unwrap it for a clean registry lookup.
    if tool.lower().startswith("functions."):
        tool = tool.split(".", 1)[1].strip()
    return tool


def _payload_from_dict(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool = ""
    for key in _TOOL_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            tool = _normalize_tool_name(val)
            break
    if not tool:
        return None

    args: Any = {}
    for key in _ARGS_KEYS:
        if key in payload:
            args = payload.get(key)
            break
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"tool": tool, "arguments": args}


def parse_mcp_payload(raw: str) -> Optional[Dict[str, Any]]:
    body = (raw or "").strip()
    if not body:
        return None

    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        result = _payload_from_dict(payload)
        if result is not None:
            return result

    tool_match = re.search(r"<tool>\s*([\s\S]*?)\s*</tool>", body, re.IGNORECASE)
    if not tool_match:
        return None
    tool = _normalize_tool_name(tool_match.group(1))
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


def _coerce_param_value(raw: str) -> Any:
    """Best-effort typing for an XML ``<parameter>`` body.

    JSON literals (objects, arrays, quoted strings, numbers, booleans) are parsed
    as-is; a bare word / phrase is kept as a string.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        pass
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except Exception:
            return text
    if re.fullmatch(r"-?\d*\.\d+", text):
        try:
            return float(text)
        except Exception:
            return text
    return text


def _parse_invoke_block(name: str, inner: str) -> Optional[Dict[str, Any]]:
    tool = _normalize_tool_name(name)
    if not tool:
        return None

    args: Dict[str, Any] = {}
    for pm in INVOKE_PARAM_RE.finditer(inner or ""):
        key = str(pm.group(1) or "").strip()
        if not key:
            continue
        args[key] = _coerce_param_value(pm.group(2))

    # Some models put a JSON object directly inside the invoke body instead of
    # emitting ``<parameter>`` tags.
    if not args:
        stripped = (inner or "").strip()
        if stripped:
            try:
                maybe = json.loads(stripped)
                if isinstance(maybe, dict):
                    args = maybe
            except Exception:
                pass

    return {"tool": tool, "arguments": args}


def extract_first_complete_mcp_call(assistant_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[re.Match]]:
    text = assistant_text or ""

    # Collect every recognised complete block, then pick whichever appears first
    # in the text so streaming truncation cuts at the right boundary regardless
    # of which syntax the model chose.
    candidates: List[Tuple[int, Dict[str, Any], re.Match]] = []

    mcp_match = MCP_CALL_BLOCK_RE.search(text)
    if mcp_match:
        payload = parse_mcp_payload(mcp_match.group(1))
        if payload:
            candidates.append((mcp_match.start(), payload, mcp_match))

    invoke_match = INVOKE_BLOCK_RE.search(text)
    if invoke_match:
        payload = _parse_invoke_block(invoke_match.group(1), invoke_match.group(2))
        if payload:
            candidates.append((invoke_match.start(), payload, invoke_match))

    tc_match = TOOL_CALL_JSON_RE.search(text)
    if tc_match:
        payload = parse_mcp_payload(tc_match.group(1))
        if payload:
            candidates.append((tc_match.start(), payload, tc_match))

    fc_match = FUNCTION_CALL_BLOCK_RE.search(text)
    if fc_match:
        payload = _parse_invoke_block(fc_match.group(1), fc_match.group(2))
        if payload:
            candidates.append((fc_match.start(), payload, fc_match))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        _, payload, match = candidates[0]
        return payload, match

    for fence_match in _FENCE_RE.finditer(text):
        payload = parse_mcp_payload(fence_match.group(1))
        if payload:
            return payload, fence_match

    return None, None


def extract_first_mcp_call(assistant_text: str) -> Optional[Dict[str, Any]]:
    payload, _ = extract_first_complete_mcp_call(assistant_text)
    return payload


def extract_all_complete_mcp_calls(
    assistant_text: str,
) -> List[Tuple[Dict[str, Any], re.Match]]:
    """Return every complete tool-call block in ``assistant_text``, in order.

    The batch counterpart of :func:`extract_first_complete_mcp_call`: a model on
    the text protocol may emit several calls in one turn, and the worker executes
    all of them before the next inference step.

    Blocks from the three recognised syntaxes are merged and sorted by position.
    Overlapping matches are dropped (an ``<mcp-call>`` wrapping a ``<tool_call>``
    would otherwise yield the same call twice). The fenced-JSON fallback only
    applies when no tagged block parsed at all, mirroring the single-call path.
    """
    text = assistant_text or ""
    candidates: List[Tuple[int, Dict[str, Any], re.Match]] = []

    for match in MCP_CALL_BLOCK_RE.finditer(text):
        payload = parse_mcp_payload(match.group(1))
        if payload:
            candidates.append((match.start(), payload, match))

    for match in INVOKE_BLOCK_RE.finditer(text):
        payload = _parse_invoke_block(match.group(1), match.group(2))
        if payload:
            candidates.append((match.start(), payload, match))

    for match in TOOL_CALL_JSON_RE.finditer(text):
        payload = parse_mcp_payload(match.group(1))
        if payload:
            candidates.append((match.start(), payload, match))

    for match in FUNCTION_CALL_BLOCK_RE.finditer(text):
        payload = _parse_invoke_block(match.group(1), match.group(2))
        if payload:
            candidates.append((match.start(), payload, match))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        calls: List[Tuple[Dict[str, Any], re.Match]] = []
        consumed_until = -1
        for start, payload, match in candidates:
            if start < consumed_until:
                continue
            calls.append((payload, match))
            consumed_until = match.end()
        return calls

    for fence_match in _FENCE_RE.finditer(text):
        payload = parse_mcp_payload(fence_match.group(1))
        if payload:
            return [(payload, fence_match)]

    return []


def strip_tool_call_blocks(text: str) -> str:
    """Remove tool-call syntax (any recognised format) from user-visible text.

    Complete blocks, leftover ``<tool_calls>`` / ``<function_calls>`` wrappers and
    a trailing unclosed tool-call tag are all removed. Fenced ``` blocks are left
    untouched so legitimate code samples survive.
    """
    out = str(text or "")
    if not out:
        return ""
    out = MCP_CALL_BLOCK_RE.sub("", out)
    out = INVOKE_BLOCK_RE.sub("", out)
    out = TOOL_CALL_JSON_RE.sub("", out)
    out = FUNCTION_CALL_BLOCK_RE.sub("", out)
    out = _WRAPPER_TAG_RE.sub("", out)
    out = _PARTIAL_TAIL_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
