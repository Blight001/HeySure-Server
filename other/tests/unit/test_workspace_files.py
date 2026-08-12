import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services.storage import screenshot_store, workspace_files
from connector_runtime.dispatch.result_payloads import normalize_screenshot_result_for_delivery
from tools import workspace_files as workspace_file_tool
from tools.workspace_files import _workspace_file_manage


def _patch_scope(monkeypatch, roots):
    def resolve(_user_id, ai_config_id, *, create=False):
        root = roots[ai_config_id]
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return str(root)

    monkeypatch.setattr(workspace_files, "member_workspace_dir", resolve)
    monkeypatch.setattr(screenshot_store, "member_workspace_dir", resolve)


def test_register_resolve_list_and_unregister_are_member_scoped(tmp_path, monkeypatch):
    roots = {2: tmp_path / "ai2", 3: tmp_path / "ai3"}
    _patch_scope(monkeypatch, roots)
    roots[2].mkdir()
    (roots[2] / "report.pdf").write_bytes(b"pdf")

    registered = workspace_files.register_workspace_file(
        user_id=1,
        ai_config_id=2,
        workspace_path="report.pdf",
    )
    resolved = workspace_files.resolve_file_ref(
        user_id=1,
        ai_config_id=2,
        file_ref=registered["file_ref"],
    )

    assert registered["file_ref"].startswith("file_")
    assert registered["workspace_path"] == "report.pdf"
    assert resolved["server_path"] == os.path.realpath(roots[2] / "report.pdf")
    assert workspace_files.list_file_refs(user_id=1, ai_config_id=2)["count"] == 1
    with pytest.raises(HTTPException) as cross_scope:
        workspace_files.resolve_file_ref(
            user_id=1,
            ai_config_id=3,
            file_ref=registered["file_ref"],
        )
    assert cross_scope.value.status_code == 404

    removed = workspace_files.unregister_file_ref(
        user_id=1,
        ai_config_id=2,
        file_ref=registered["file_ref"],
    )
    assert removed["file_deleted"] is False
    assert (roots[2] / "report.pdf").exists()


def test_absolute_and_traversal_paths_are_rejected(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    _patch_scope(monkeypatch, {2: root})

    for path in (str(outside), "../secret.txt"):
        with pytest.raises(HTTPException) as raised:
            workspace_files.register_workspace_file(
                user_id=1,
                ai_config_id=2,
                workspace_path=path,
            )
        assert raised.value.status_code == 403


def test_tampered_reference_cannot_escape_workspace(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    _patch_scope(monkeypatch, {2: root})
    registered = workspace_files.register_workspace_file(
        user_id=1, ai_config_id=2, workspace_path="safe.txt"
    )
    metadata = root / ".heysure" / "file_refs" / f"{registered['file_ref']}.json"
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["workspace_path"] = "../secret.txt"
    metadata.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(HTTPException) as raised:
        workspace_files.resolve_file_ref(
            user_id=1, ai_config_id=2, file_ref=registered["file_ref"]
        )
    assert raised.value.status_code == 403


def test_screenshot_is_saved_in_member_workspace_and_returns_file_ref(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    _patch_scope(monkeypatch, {2: root})
    normalized = normalize_screenshot_result_for_delivery(
        "aifree.browser+screenshot",
        {"success": True, "dataUrl": "data:image/png;base64,UE5H"},
        {"send_to_user": False},
    )

    persisted = screenshot_store.attach_persisted_screenshot(
        user_id=1,
        ai_config_id=2,
        tool="aifree.browser+screenshot",
        result=normalized,
    )

    assert persisted["send_to_user"] is False
    assert persisted["file_ref"].startswith("file_")
    assert persisted["workspace_path"].startswith("Screenshots/")
    assert os.path.isfile(persisted["server_path"])


def test_mcp_register_returns_small_model_send_example(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    (root / "result.zip").write_bytes(b"zip")
    _patch_scope(monkeypatch, {2: root})

    result = _workspace_file_manage(
        1,
        {"action": "register", "workspace_path": "result.zip"},
        2,
    )

    assert result["send_example"]["arguments"]["attachments"][0]["file_ref"] == result["file_ref"]


def test_mcp_view_image_registers_path_and_returns_private_model_marker(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    (root / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
    _patch_scope(monkeypatch, {2: root})

    result = _workspace_file_manage(
        1,
        {"action": "view_image", "workspace_path": "cat.png"},
        2,
    )

    assert result["_heysure_model_image"] is True
    assert result["mime_type"] == "image/png"
    assert result["file_ref"].startswith("file_")
    assert result["server_path"] == os.path.realpath(root / "cat.png")


def test_mcp_view_image_rejects_non_image_content(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    (root / "fake.png").write_bytes(b"not an image")
    _patch_scope(monkeypatch, {2: root})

    with pytest.raises(HTTPException) as raised:
        _workspace_file_manage(
            1,
            {"action": "view_image", "workspace_path": "fake.png"},
            2,
        )

    assert raised.value.detail["code"] == "UNSUPPORTED_IMAGE"
    assert workspace_files.list_file_refs(user_id=1, ai_config_id=2)["count"] == 0


def test_mcp_view_image_rejects_images_over_model_limit(tmp_path, monkeypatch):
    root = tmp_path / "ai2"
    root.mkdir()
    image = root / "large.png"
    with image.open("wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.seek(workspace_file_tool.MAX_MODEL_IMAGE_BYTES)
        handle.write(b"x")
    _patch_scope(monkeypatch, {2: root})

    with pytest.raises(HTTPException) as raised:
        _workspace_file_manage(
            1,
            {"action": "view_image", "workspace_path": "large.png"},
            2,
        )

    assert raised.value.detail["code"] == "IMAGE_TOO_LARGE"


def test_mcp_creates_and_revokes_default_five_minute_link(monkeypatch):
    created_calls = []
    monkeypatch.setattr(
        workspace_file_tool,
        "configured_public_base_url",
        lambda: "https://files.example",
    )
    monkeypatch.setattr(
        workspace_file_tool,
        "create_temporary_file_link",
        lambda **kwargs: created_calls.append(kwargs) or {
            "grant_id": "fgrant_" + "a" * 32,
            "url": "https://files.example/api/tmp-files/grant/token",
        },
    )
    created = _workspace_file_manage(
        1,
        {"action": "create_temporary_link", "file_ref": "file_" + "b" * 32},
        2,
    )
    assert created["grant_id"].startswith("fgrant_")
    assert created_calls[0]["ttl_seconds"] == 300

    revoked_calls = []
    monkeypatch.setattr(
        workspace_file_tool,
        "revoke_temporary_file_link",
        lambda **kwargs: revoked_calls.append(kwargs) or {"revoked": True},
    )
    revoked = _workspace_file_manage(
        1,
        {"action": "revoke_temporary_link", "grant_id": created["grant_id"]},
        2,
    )
    assert revoked["revoked"] is True
    assert revoked_calls[0]["user_id"] == 1
    assert revoked_calls[0]["ai_config_id"] == 2


def test_import_chat_media_saves_bytes_without_exposing_database_blob(monkeypatch):
    row = SimpleNamespace(
        user_id=1,
        token="secret-token",
        media_type="image/png",
        data=b"PNG",
    )

    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _model, media_id):
            assert media_id == 8
            return row

    monkeypatch.setattr(workspace_file_tool, "Session", FakeSession)
    monkeypatch.setattr(
        workspace_file_tool,
        "save_workspace_bytes",
        lambda **kwargs: {
            "file_ref": "file_" + "c" * 32,
            "workspace_path": "Imported/chat.png",
            "file_name": kwargs["file_name"],
        },
    )

    result = _workspace_file_manage(
        1,
        {"action": "import_chat_media", "media_id": 8, "media_token": "secret-token"},
        2,
    )

    assert result["file_name"] == "chat_media_8.png"
    assert "data" not in result
    assert result["send_example"]["arguments"]["attachments"][0]["file_ref"] == result["file_ref"]
