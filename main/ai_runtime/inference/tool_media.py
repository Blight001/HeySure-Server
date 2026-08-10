"""Screenshot payload, model-image context, and display helpers."""

import base64
import os
from typing import Dict, List, Optional

from api.chat_runtime.chat_prompt_utils import _sanitize_large_media
from api.services.mcp.mcp_tool_media import canonical_screenshot_tool_name


_IMAGE_DATA_URL_KEYS = (
    "dataUrl",
    "data_url",
    "imageDataUrl",
    "screenshotDataUrl",
    "screenshot",
)
_IMAGE_URL_KEYS = ("image_url", "public_url")
_IMAGE_PATH_KEYS = ("server_path", "path")
_NESTED_PAYLOAD_KEYS = ("result", "payload", "data", "screenshot_result")


def is_image_input_unsupported_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    image_markers = (
        "image_url",
        "image input",
        "image content",
        "images are",
        "multimodal",
        "vision input",
        "invalid image",
        "image is not supported",
    )
    incompatibility_markers = (
        "unknown variant",
        "expected text",
        "not support",
        "unsupported",
        "only supports text",
        "text-only",
        "invalid content type",
        "failed to deserialize",
        "does not support image",
    )
    return any(marker in text for marker in image_markers) and any(
        marker in text for marker in incompatibility_markers
    )


def _without_image_blocks(content: list, replacement: str) -> tuple[list, int]:
    kept = []
    removed = 0
    for block in content:
        block_type = (
            str(block.get("type") or "").lower()
            if isinstance(block, dict)
            else ""
        )
        if block_type in {"image", "image_url"}:
            removed += 1
        else:
            kept.append(block)
    if removed:
        kept.append({"type": "text", "text": replacement.format(count=removed)})
    return kept, removed


def degrade_image_messages_to_text(conversation: List[Dict]) -> int:
    removed = 0
    replacement = "[系统已省略 {count} 张图片：当前模型不支持图片输入。]"
    for message in conversation:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        message["content"], message_removed = _without_image_blocks(
            content,
            replacement,
        )
        removed += message_removed
    return removed


def prune_prior_runtime_screenshot_images(conversation: List[Dict]) -> int:
    """Remove image blocks only from earlier runtime screenshot messages."""

    removed = 0
    replacement = "[系统已省略 {count} 张较早截图：模型上下文只保留最新截图。]"
    markers = ("工具截图已捕获", "鼠标点击前确认图已捕获")
    for message in conversation:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict)
            and str(block.get("type") or "").lower() == "text"
        )
        if not any(marker in text for marker in markers):
            continue
        message["content"], message_removed = _without_image_blocks(
            content,
            replacement,
        )
        removed += message_removed
    return removed


def image_input_degraded_feedback(error_text: str, removed_images: int) -> str:
    return "\n".join([
        "[运行时图片输入错误]",
        f"上游模型拒绝了图片输入，系统已从模型上下文中移除 {removed_images} 张图片。",
        "你无法查看这些图片。请基于已有文字、工具返回的非图片信息继续执行；",
        "如果任务必须依赖视觉内容，请明确说明该限制或改用不需要视觉输入的工具，不要假装已经看过图片。",
        "",
        "[上游错误]",
        str(error_text or "").strip(),
    ])


def _first_text(mapping: dict, keys: tuple[str, ...], predicate) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and predicate(value.strip()):
            return value.strip()
    return ""


def _child_payloads(value: object):
    if isinstance(value, dict):
        yielded = set()
        for key in _NESTED_PAYLOAD_KEYS:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yielded.add(id(child))
                yield child
        for child in value.values():
            if isinstance(child, (dict, list)) and id(child) not in yielded:
                yield child
    elif isinstance(value, list):
        yield from value


def find_image_payload(value: object, depth: int = 0) -> Dict[str, str]:
    if depth > 5:
        return {}
    if isinstance(value, str):
        text = value.strip()
        return {"data_url": text} if text.startswith("data:image/") else {}
    if isinstance(value, dict):
        data_url = _first_text(value, _IMAGE_DATA_URL_KEYS, lambda text: text.startswith("data:image/"))
        if data_url:
            return {"data_url": data_url}
        url = _first_text(value, _IMAGE_URL_KEYS, lambda text: text.startswith(("http://", "https://")))
        if url:
            return {"url": url}
        path = _first_text(value, _IMAGE_PATH_KEYS, bool)
        if path:
            return {"path": path}
    for child in _child_payloads(value):
        found = find_image_payload(child, depth + 1)
        if found:
            return found
    return {}


def image_path_to_data_url(path: str) -> str:
    server_path = str(path or "").strip()
    if not server_path or not os.path.isfile(server_path):
        return ""
    extension = os.path.splitext(server_path)[1].lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(extension, "image/png")
    try:
        with open(server_path, "rb") as file_handle:
            encoded = base64.b64encode(file_handle.read()).decode("ascii")
        return f"data:{media_type};base64,{encoded}"
    except OSError:
        return ""


def omit_image_fields(value: object) -> object:
    omitted = {*_IMAGE_DATA_URL_KEYS, "server_path", "workspace_path"}
    if isinstance(value, dict):
        return {
            key: omit_image_fields(item)
            for key, item in value.items()
            if key not in omitted
        }
    if isinstance(value, list):
        return [omit_image_fields(item) for item in value]
    return value


def tool_image_message(
    tool: str,
    tool_result: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if (
        not canonical_screenshot_tool_name(tool, include_mouse_click=True)
        or not isinstance(tool_result, dict)
    ):
        return None
    result_payload = tool_result.get("result", tool_result)
    image_payload = find_image_payload(tool_result)
    public_url = image_payload.get("url", "")
    data_url = image_payload.get("data_url", "")
    if not data_url.startswith("data:image/"):
        data_url = image_path_to_data_url(image_payload.get("path", ""))
    if not data_url.startswith("data:image/") and not public_url.startswith(
        ("http://", "https://")
    ):
        return None
    metadata = result_payload if isinstance(result_payload, dict) else {}
    detail = "\n".join(
        part
        for part in [
            "工具截图已捕获。你已经收到这张图片，请直接查看视觉内容并继续，不要让用户打开本地路径。",
            f"URL: {metadata.get('url') or ''}".strip(),
            f"Method: {metadata.get('method') or ''}".strip(),
        ]
        if part and not part.endswith(":")
    )
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": detail},
            {"type": "image_url", "image_url": {"url": public_url or data_url}},
        ],
    }


def screenshot_display_ref(
    tool: str,
    tool_result: Dict[str, object],
) -> Dict[str, str]:
    if (
        not canonical_screenshot_tool_name(tool, include_mouse_click=True)
        or not isinstance(tool_result, dict)
    ):
        return {}
    payload = find_image_payload(tool_result)
    url = payload.get("url", "")
    if url.startswith(("http://", "https://")):
        return {"url": url}
    data_url = payload.get("data_url", "")
    if not data_url.startswith("data:image/"):
        data_url = image_path_to_data_url(payload.get("path", ""))
    return {"data_url": data_url} if data_url.startswith("data:image/") else {}


def find_screenshot_result_payload(
    value: object,
    depth: int = 0,
) -> Dict[str, object]:
    if depth > 5:
        return {}
    if isinstance(value, dict) and any(
        key in value
        for key in (
            "send_to_user",
            "bot_send_to_user",
            "deliver_to_user",
            "save_to_server",
            "server_path",
        )
    ):
        return value
    for child in _child_payloads(value):
        found = find_screenshot_result_payload(child, depth + 1)
        if found:
            return found
    return {}


def screenshot_send_to_user_enabled(
    tool: str,
    tool_result: Dict[str, object],
    args: Optional[dict] = None,
) -> bool:
    tool_kind = canonical_screenshot_tool_name(tool)
    if not tool_kind:
        return False
    if isinstance(args, dict) and any(
        key in args and args.get(key) is False
        for key in ("send_to_user", "bot_send_to_user", "deliver_to_user")
    ):
        return False
    payload = find_screenshot_result_payload(tool_result)
    return (
        payload.get("send_to_user") is True
        or payload.get("bot_send_to_user") is True
        or payload.get("deliver_to_user") is True
        or bool(tool_kind)
    )


def model_visible_tool_result(
    tool: str,
    tool_result: Dict[str, object],
    *,
    image_attached: bool = True,
) -> object:
    result_payload = (
        tool_result.get("result", tool_result)
        if isinstance(tool_result, dict)
        else tool_result
    )
    if (
        not canonical_screenshot_tool_name(tool, include_mouse_click=True)
        or not isinstance(result_payload, dict)
    ):
        return result_payload
    cleaned = omit_image_fields(_sanitize_large_media(result_payload))
    if not isinstance(cleaned, dict):
        cleaned = {}
    cleaned["screenshot_attached_to_model"] = image_attached
    if not image_attached:
        cleaned["instruction"] = (
            "The screenshot could not be attached because the current model does not support image input. "
            "Continue from non-image tool data, use a non-visual tool, or explain the limitation."
        )
        return cleaned
    cleaned["instruction"] = (
        "The screenshot image is attached in a following user message. "
        "Analyze the image directly; do not ask the user to open a local path."
    )
    return cleaned
