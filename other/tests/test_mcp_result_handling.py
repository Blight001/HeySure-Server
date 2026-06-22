from ai_runtime.inference.core import _tool_result_failed


def test_tool_result_failed_preserves_structured_failure_detail():
    failed, detail = _tool_result_failed({
        "tool": "workspace.run_command",
        "result": {
            "success": False,
            "failure_type": "nonzero_exit",
            "exit_code": 3,
            "stderr": "rg not found",
            "stdout": "",
        },
    })

    assert failed is True
    assert "nonzero_exit" in detail
    assert "exit_code=3" in detail
    assert "rg not found" in detail
