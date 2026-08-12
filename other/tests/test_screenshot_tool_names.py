from ai_runtime.inference.tool_media import (
    model_visible_tool_result,
    tool_image_message,
)
from api.services.mcp.mcp_tool_media import canonical_screenshot_tool_name
from connector_runtime.dispatch.result_payloads import (
    normalize_screenshot_result_for_delivery as _normalize_screenshot_result_for_delivery,
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

    message = tool_image_message(AI_FREE_SCREENSHOT, tool_result)
    visible_result = model_visible_tool_result(
        AI_FREE_SCREENSHOT,
        tool_result,
        image_attached=True,
    )

    assert message is not None
    assert message["content"][1]["image_url"]["url"] == DATA_URL
    assert "dataUrl" not in visible_result
    assert visible_result["screenshot_attached_to_model"] is True


def test_server_path_screenshot_is_encoded_for_model(tmp_path):
    screenshot = tmp_path / "capture.png"
    screenshot.write_bytes(b"PNG")

    message = tool_image_message(
        AI_FREE_SCREENSHOT,
        {"result": {"success": True, "server_path": str(screenshot)}},
    )

    assert message is not None
    assert message["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_workspace_view_image_is_attached_without_exposing_server_path(tmp_path):
    image = tmp_path / "cat.png"
    image.write_bytes(b"PNG")
    tool_result = {
        "result": {
            "success": True,
            "_heysure_model_image": True,
            "file_ref": "file_" + "a" * 32,
            "file_name": "cat.png",
            "workspace_path": "Uploads/cat.png",
            "server_path": str(image),
        },
    }

    message = tool_image_message("workspace.file+manage", tool_result)
    visible = model_visible_tool_result(
        "workspace.file+manage", tool_result, image_attached=True,
    )

    assert message is not None
    assert message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "server_path" not in visible
    assert visible["workspace_path"] == "Uploads/cat.png"
    assert visible["image_attached_to_model"] is True
    assert "_heysure_model_image" not in visible


def test_workspace_view_image_uses_validated_mime_instead_of_file_extension(tmp_path):
    image = tmp_path / "download.bin"
    image.write_bytes(b"JPEG")
    message = tool_image_message(
        "workspace.file+manage",
        {"result": {
            "_heysure_model_image": True,
            "mime_type": "image/jpeg",
            "server_path": str(image),
        }},
    )

    assert message["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_namespaced_ai_free_screenshot_honors_send_to_user_delivery():
    result = {"success": True, "dataUrl": DATA_URL}

    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        result,
        {"send_to_user": True},
    )

    assert normalized["send_to_user"] is True
    assert normalized["save_to_server"] is True


def test_explicit_send_to_user_false_prevents_delivery_but_still_saves():
    result = {"success": True, "dataUrl": DATA_URL, "send_to_user": True}

    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        result,
        {"send_to_user": False},
    )

    assert normalized["send_to_user"] is False
    assert normalized["save_to_server"] is True


def test_namespaced_ai_free_screenshot_defaults_to_save_and_send():
    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        {"success": True, "dataUrl": DATA_URL},
        {},
    )

    assert normalized["send_to_user"] is True
    assert normalized["save_to_server"] is True


def test_explicit_save_false_is_independent_from_send_setting():
    normalized = _normalize_screenshot_result_for_delivery(
        AI_FREE_SCREENSHOT,
        {"success": True, "dataUrl": DATA_URL},
        {"save_to_workspace": False, "send_to_user": True},
    )

    assert normalized["send_to_user"] is True
    assert "save_to_server" not in normalized
