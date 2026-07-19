"""A turn that emits several identical tool calls must execute each only once.

Models occasionally repeat the exact same (tool + arguments) call inside one
turn. The worker runs the first occurrence and answers the duplicates without
re-executing, so a side-effecting tool does not fire twice off one hiccup.
``_duplicate_call_flags`` is the pure decision the batch loop consults.
"""

from ai_runtime.inference.core import _duplicate_call_flags


def _call(tool, **args):
    return {"id": f"call_{tool}", "tool": tool, "arguments": args}


def test_no_duplicates_all_false():
    calls = [_call("a.one", x=1), _call("a.two", x=1), _call("a.one", x=2)]
    assert _duplicate_call_flags(calls) == [False, False, False]


def test_exact_duplicate_flagged_after_first():
    calls = [_call("msg.send", text="hi"), _call("msg.send", text="hi")]
    assert _duplicate_call_flags(calls) == [False, True]


def test_three_identical_only_first_survives():
    calls = [_call("click", target="ok")] * 3
    assert _duplicate_call_flags(calls) == [False, True, True]


def test_argument_key_order_does_not_matter():
    calls = [
        {"tool": "t", "arguments": {"a": 1, "b": 2}},
        {"tool": "t", "arguments": {"b": 2, "a": 1}},
    ]
    assert _duplicate_call_flags(calls) == [False, True]


def test_different_arguments_are_not_duplicates():
    calls = [_call("t", x=1), _call("t", x=2)]
    assert _duplicate_call_flags(calls) == [False, False]


def test_same_args_different_tool_are_not_duplicates():
    calls = [_call("a", x=1), _call("b", x=1)]
    assert _duplicate_call_flags(calls) == [False, False]


def test_empty_and_missing_arguments_treated_equal():
    calls = [
        {"tool": "t", "arguments": {}},
        {"tool": "t"},
        {"tool": "t", "arguments": None},
    ]
    assert _duplicate_call_flags(calls) == [False, True, True]


def test_duplicate_then_unique_then_duplicate():
    calls = [_call("t", x=1), _call("t", x=1), _call("u", y=2), _call("u", y=2)]
    assert _duplicate_call_flags(calls) == [False, True, False, True]
