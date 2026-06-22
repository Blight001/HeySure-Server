from ai_runtime.inference.core import _prune_prior_runtime_screenshot_images


def test_prune_prior_runtime_screenshot_images_only_removes_screenshot_blocks():
    convo = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "工具截图已捕获。你已经收到这张图片。"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,old"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "用户上传的参考图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,user"}},
            ],
        },
    ]

    removed = _prune_prior_runtime_screenshot_images(convo)

    assert removed == 1
    assert all(block.get("type") != "image_url" for block in convo[0]["content"])
    assert "模型上下文只保留最新截图" in convo[0]["content"][-1]["text"]
    assert convo[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,user"
