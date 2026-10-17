from ai_runtime.inference.tool_media import generated_media_attachment_records


def test_infinite_canvas_generated_files_are_extracted_from_nested_dispatch_result():
    result = {
        "result": {
            "result": {
                "uploaded_files": [
                    {
                        "file_ref": "file_" + "a" * 32,
                        "workspace_path": "Uploads/generated.png",
                        "file_name": "generated.png",
                        "mime_type": "image/png",
                        "bytes": 42,
                    },
                    {
                        "file_ref": "file_" + "b" * 32,
                        "workspace_path": "Uploads/generated.mp4",
                        "file_name": "generated.mp4",
                        "mime_type": "video/mp4",
                        "bytes": 84,
                    },
                ]
            }
        }
    }

    records = generated_media_attachment_records("canvas_generation_image", result)

    assert [item["file_name"] for item in records] == ["generated.png", "generated.mp4"]
    assert records[1]["mime_type"] == "video/mp4"


def test_generated_media_rejects_non_media_or_unsafe_endpoint_output():
    result = {
        "uploaded_files": [
            {"file_ref": "file_" + "a" * 32, "workspace_path": "../secret.txt", "mime_type": "text/plain"},
            {"file_ref": "file_" + "b" * 32, "workspace_path": "/absolute/video.mp4", "mime_type": "video/mp4"},
        ]
    }

    assert generated_media_attachment_records("canvas_generation_video", result) == []
    assert generated_media_attachment_records("workspace.file+manage", result) == []


def test_current_infinite_canvas_connector_attachment_shape_is_supported():
    result = {
        "result": {
            "heysure_attachments": [{
                "file_ref": "file_" + "c" * 32,
                "workspace_path": "Uploads/generated.png",
                "mime_type": "image/png",
                "kind": "image",
            }],
        },
    }

    records = generated_media_attachment_records("canvas_generation_image", result)

    assert records[0]["file_name"] == "generated.png"
    assert records[0]["mime_type"] == "image/png"
