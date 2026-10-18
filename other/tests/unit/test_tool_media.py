from ai_runtime.inference.tool_media import (
    generated_media_attachment_records,
    workspace_attachment_records,
)


def test_prompt_library_references_are_extracted_for_workspace_import():
    result = {
        "data": {
            "items": [{
                "name": "示例",
                "reference_image": "/api/gpt-image-2/images/case1.jpg",
                "reference_images": ["/api/gpt-image-2/images/case1.jpg"],
            }],
        },
    }

    records = generated_media_attachment_records("canvas_prompt_list", result)

    assert len(records) == 1
    assert records[0]["source_url"] == "/api/gpt-image-2/images/case1.jpg"
    assert records[0]["file_name"] == "case1.jpg"


def test_workspace_attachment_records_are_mime_agnostic():
    result = {
        "heysure_attachments": [{
            "file_ref": "file_" + "d" * 32,
            "workspace_path": "Uploads/report.pdf",
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
            "bytes": 128,
        }],
    }

    records = workspace_attachment_records(result)

    assert records[0]["mime_type"] == "application/pdf"
    assert records[0]["resource_kind"] == "file"


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
