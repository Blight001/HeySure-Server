"""Regression tests for tolerant parsing of model-emitted MCP tool calls.

The model is told to emit ``<mcp-call>{...}</mcp-call>`` but frequently falls
back to other tool-calling syntaxes it was pre-trained on. These tests lock in
that every recognised variant normalises to ``{"tool", "arguments"}`` and that
the block is stripped from user-visible text.
"""

from api.chat_runtime.mcp_parser import (
    extract_first_complete_mcp_call,
    extract_first_mcp_call,
    strip_tool_call_blocks,
)


def test_canonical_mcp_call_block():
    text = '<mcp-call>{"tool":"workspace.search","arguments":{"query":"x"}}</mcp-call>'
    assert extract_first_mcp_call(text) == {
        "tool": "workspace.search",
        "arguments": {"query": "x"},
    }


def test_namespaced_invoke_with_parameters():
    text = (
        "好的，我来查一下。\n"
        "<mcp:tool_calls>\n"
        '<mcp:invoke name="mcp.describe+tool">\n'
        '<mcp:parameter name="name">workspace.search</mcp:parameter>\n'
        "</mcp:invoke>\n"
        "</mcp:tool_calls>"
    )
    assert extract_first_mcp_call(text) == {
        "tool": "mcp.describe+tool",
        "arguments": {"name": "workspace.search"},
    }


def test_plain_invoke_coerces_scalar_parameters():
    text = (
        "<function_calls><invoke name=\"workspace.search\">"
        '<parameter name="query">hello</parameter>'
        '<parameter name="limit">5</parameter>'
        "</invoke></function_calls>"
    )
    assert extract_first_mcp_call(text) == {
        "tool": "workspace.search",
        "arguments": {"query": "hello", "limit": 5},
    }


def test_invoke_with_json_body():
    text = '<invoke name="task.manage">{"action":"list"}</invoke>'
    assert extract_first_mcp_call(text) == {
        "tool": "task.manage",
        "arguments": {"action": "list"},
    }


def test_hermes_tool_call_uses_name_alias():
    text = '<tool_call>{"name":"workspace.search","arguments":{"query":"y"}}</tool_call>'
    assert extract_first_mcp_call(text) == {
        "tool": "workspace.search",
        "arguments": {"query": "y"},
    }


def test_fenced_json_with_parameters_alias():
    text = '```json\n{"name":"mcp.describe+tool","parameters":{"name":"browser.open"}}\n```'
    assert extract_first_mcp_call(text) == {
        "tool": "mcp.describe+tool",
        "arguments": {"name": "browser.open"},
    }


def test_functions_prefix_is_unwrapped():
    text = '<invoke name="functions.workspace.search"><parameter name="query">z</parameter></invoke>'
    assert extract_first_mcp_call(text) == {
        "tool": "workspace.search",
        "arguments": {"query": "z"},
    }


def test_no_tool_call_returns_none():
    assert extract_first_mcp_call("just some normal prose, no tools here") is None


def test_match_end_covers_full_invoke_block_for_streaming_truncation():
    prefix = "Answer text. "
    block = (
        '<mcp:invoke name="workspace.search">'
        '<mcp:parameter name="query">x</mcp:parameter>'
        "</mcp:invoke>"
    )
    payload, match = extract_first_complete_mcp_call(prefix + block + " trailing")
    assert payload is not None
    # Streaming truncates at match.end(); it must land right after the block.
    assert (prefix + block + " trailing")[: match.end()] == prefix + block


def test_strip_removes_invoke_block_keeps_prose():
    text = (
        "Answer here.\n"
        "<mcp:tool_calls>\n"
        '<mcp:invoke name="mcp.describe+tool">\n'
        '<mcp:parameter name="name">workspace.search</mcp:parameter>\n'
        "</mcp:invoke>\n"
        "</mcp:tool_calls>"
    )
    assert strip_tool_call_blocks(text) == "Answer here."


def test_strip_removes_unclosed_streaming_tail():
    text = 'Thinking...\n<mcp:invoke name="workspace.search"><parameter name="q">a'
    assert strip_tool_call_blocks(text) == "Thinking..."


def test_grok_function_call_block_with_parameters():
    text = (
        "我先查一下天气。\n"
        '<xai:function_call name="weather.query">'
        '<parameter name="city">北京</parameter>'
        '<parameter name="days">3</parameter>'
        "</xai:function_call>"
    )
    assert extract_first_mcp_call(text) == {
        "tool": "weather.query",
        "arguments": {"city": "北京", "days": 3},
    }
    assert strip_tool_call_blocks(text) == "我先查一下天气。"


def test_grok_function_call_block_with_json_body():
    text = '<function_call name="task.manage">{"action":"list"}</function_call>'
    assert extract_first_mcp_call(text) == {
        "tool": "task.manage",
        "arguments": {"action": "list"},
    }


def test_grok_blocks_extracted_in_batch():
    from api.chat_runtime.mcp_parser import extract_all_complete_mcp_calls

    text = (
        '<xai:function_call name="a.one"><parameter name="k">v</parameter></xai:function_call>'
        "middle text"
        '<xai:function_call name="b.two"></xai:function_call>'
    )
    calls = extract_all_complete_mcp_calls(text)
    assert [payload["tool"] for payload, _match in calls] == ["a.one", "b.two"]
    assert calls[0][0]["arguments"] == {"k": "v"}


def test_plural_function_calls_wrapper_still_parses_inner_invoke():
    # The attribute-less plural wrapper must stay out of the grok branch so the
    # Anthropic-style inner <invoke> keeps winning.
    text = (
        "<function_calls>"
        '<invoke name="workspace.search"><parameter name="query">q</parameter></invoke>'
        "</function_calls>"
    )
    assert extract_first_mcp_call(text) == {
        "tool": "workspace.search",
        "arguments": {"query": "q"},
    }


def test_strip_removes_grok_block_and_keeps_trailing_prose():
    text = (
        "先看一眼。"
        '<xai:function_call name="a.b"><parameter name="x">1</parameter></xai:function_call>'
        "然后继续。"
    )
    assert strip_tool_call_blocks(text) == "先看一眼。然后继续。"
