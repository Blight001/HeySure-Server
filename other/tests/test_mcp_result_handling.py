from ai_runtime.inference.tool_execution import tool_result_failed
from api.services.workflows.result_store import device_step_error


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


def test_tool_result_failed_detects_nested_endpoint_business_failure():
    failed, detail = tool_result_failed({
        "tool": "aifree.manage+card",
        "result": {
            "success": True,
            "result": {
                "success": False,
                "errorCode": "CARD_STEP_FAILED",
                "error": "自动化步骤执行失败",
            },
        },
    })

    assert failed is True
    assert detail == "CARD_STEP_FAILED: 自动化步骤执行失败"


def test_device_step_error_preserves_nested_code_after_transport_promotion():
    error = device_step_error(
        success=False,
        result={
            "success": False,
            "errorCode": "CARD_STEP_FAILED",
            "error": "自动化步骤执行失败",
        },
        transport_error="agent reported failure",
    )

    assert error == {
        "code": "CARD_STEP_FAILED",
        "message": "自动化步骤执行失败",
        "phase": "device_result",
        "retryable": False,
    }
