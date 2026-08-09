from ai_runtime.inference.tool_execution import tool_result_failed


def test_tool_result_failed_preserves_structured_failure_detail():
    failed, detail = tool_result_failed({
        "tool": "workspace.run+command",
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
