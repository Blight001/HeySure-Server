"""A turn with tool-call-looking markup that parsed to nothing must warn the
model instead of silently ending the run.

Grok-style private syntax (or a mangled block) used to fall through: the
frontend strips the markup, so the user saw the reply stop right after the
model announced its plan. The fallback warning is allowed once per run so
prose that merely discusses tool-call syntax cannot loop the warning forever.
"""

from api.chat_runtime.chat_prompt_utils import _build_mcp_stream_warning


def test_unparsed_markup_triggers_fallback_warning():
    text = "我现在来提交表单。\n<my:tool_call name=\"browser.click\"><arg>提交</arg>"
    warning = _build_mcp_stream_warning(text, None, "")
    assert warning
    assert "<mcp-call>" in warning


def test_plain_prose_returns_none():
    assert _build_mcp_stream_warning("全部搞定了，没有更多动作。", None, "") is None


def test_fallback_disabled_after_first_use():
    text = '<xai:function_call name="a.b">'
    assert _build_mcp_stream_warning(text, None, "", markup_fallback=False) is None


def test_malformed_mcp_call_block_still_warns_regardless_of_fallback_flag():
    text = "<mcp-call>not json at all</mcp-call>"
    warning = _build_mcp_stream_warning(text, None, "", markup_fallback=False)
    assert warning
