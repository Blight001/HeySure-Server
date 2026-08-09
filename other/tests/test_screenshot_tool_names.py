from ai_runtime.inference.core import (
    _browser_screenshot_image_message,
    _model_visible_tool_result,
)
from api.services.mcp.mcp_tool_media import canonical_screenshot_tool_name
from connector_runtime.dispatch.device_dispatch import (
    _normalize_screenshot_result_for_delivery,
)


AI_FREE_SCREENSHOT = "aifree.browser+screenshot"
DATA_URL = "data:image/png;base64,U0NSRUVOU0hPVA=="


def test_namespaced_ai_free_tool_is_classified_as_browser_screenshot():
    assert canonical_screenshot_tool_name(AI_FREE_SCREENSHOT) == "browser_screenshot"
    assert canonical_screenshot_tool_name("aifree.browser_screenshot") == "browser_screenshot"
    assert canonical_screenshot_tool_name("aifree.browser+observe") == ""


def test_namespaced_ai_free_screenshot_is_attached_as_model_image():
    tool_result = {
        "result": {
            "success": True,
            "dataUrl": DATA_URL,
            "width": 1280,
            "height": 720,
        },
    }

    message = _browser_screenshot_image_message(AI_FREE_SCREENSHOT, tool_result)
    visible_result = _model_visible_tool_result(
        AI_FREE_SCREENSHOT,
        tool_result,
        image_attached=True,
    )

    assert message is not None
    assert message["content"][1]["image_url"]["url"] == DATA_URL
    assert "dataUrl" not in visible_result
    assert visible_result["screenshot_attached_to_model"] is True


def test_namespaced_ai_free_screenshot_honors_send_to_user_delivery():
    result = {"success": True, "dataUrl": DATA_URL}

    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        result,
        {"send_to_user": True},
    )

    assert normalized["send_to_user"] is True
    assert normalized["save_to_server"] is True


def test_explicit_send_to_user_false_still_prevents_delivery():
    result = {"success": True, "dataUrl": DATA_URL, "send_to_user": True}

    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        result,
        {"send_to_user": False},
    )

    assert normalized == result
    assert "save_to_server" not in normalized
