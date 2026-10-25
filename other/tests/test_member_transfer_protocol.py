import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.storage import member_transfer, workspace_files
from gateway.routers.member_transfer_routes import router
from mcp_runtime.mcp import registry


class _Request:
    def __init__(self, chunks):
        self.headers = {"content-length": str(sum(len(chunk) for chunk in chunks))}
        self._chunks = list(chunks)

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def _sandbox(monkeypatch, tmp_path):
    root = str(tmp_path / "workspace")
    Path(root).mkdir()
    monkeypatch.setattr(member_transfer, "TRANSFER_DIR", tmp_path / "transfers")
    monkeypatch.setattr(member_transfer, "member_workspace_dir", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(workspace_files, "member_workspace_dir", lambda *_args, **_kwargs: root)
    return root


def test_transfer_tool_is_the_single_catalog_entry():
    tool = registry.get("workspace.transfer")
    assert tool.input_schema["properties"]["action"]["enum"] == [
        "prepare_download",
        "prepare_upload",
        "commit",
        "status",
        "abort",
    ]
    assert "base64" in tool.description


def test_upload_commit_uses_stream_and_returns_file_ref(monkeypatch, tmp_path):
    root = _sandbox(monkeypatch, tmp_path)
    prepared = member_transfer.prepare_upload(
        user_id=7,
        ai_config_id=11,
        file_name="冰之女皇.png",
        mime_type="image/png",
        folder="原神",
        public_base_url="https://example.test",
    )
    transfer_id = prepared["transfer_id"]
    url_token = prepared["transport"]["url"].rsplit("/", 1)[-1]
    record_path = member_transfer.TRANSFER_DIR / f"{transfer_id}.json"
    assert "token" not in json.loads(record_path.read_text(encoding="utf-8"))

    uploaded = asyncio.run(
        member_transfer.receive_upload(
            transfer_id,
            url_token,
            _Request([b"png", b"-bytes"]),
        )
    )
    assert uploaded["state"] == "uploaded"
    committed = member_transfer.commit_upload(
        user_id=7,
        ai_config_id=11,
        transfer_id=transfer_id,
        expected_bytes=uploaded["bytes"],
        expected_sha256=uploaded["sha256"],
        public_base_url="https://example.test",
    )
    assert committed["state"] == "committed"
    assert committed["object"]["ref"].startswith("file_")
    assert Path(root, committed["workspace_path"]).is_file()

    with pytest.raises(HTTPException) as raised:
        member_transfer.commit_upload(
            user_id=7,
            ai_config_id=11,
            transfer_id=transfer_id,
            public_base_url="https://example.test",
        )
    assert raised.value.status_code == 409


def test_prepare_download_never_exposes_server_path(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    saved = workspace_files.save_workspace_bytes(
        user_id=7,
        ai_config_id=11,
        data=b"hello",
        file_name="hello.txt",
    )
    envelope = member_transfer.prepare_download(
        user_id=7,
        ai_config_id=11,
        file_ref=saved["file_ref"],
        public_base_url="https://example.test",
    )
    assert envelope["protocol"] == member_transfer.PROTOCOL
    assert envelope["direction"] == "server_to_device"
    assert envelope["object"]["ref"] == saved["file_ref"]
    assert "server_path" not in json.dumps(envelope)


def test_http_get_streams_a_prepared_server_file(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    saved = workspace_files.save_workspace_bytes(
        user_id=7,
        ai_config_id=11,
        data=b"download-me",
        file_name="download.txt",
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    prepared = member_transfer.prepare_download(
        user_id=7,
        ai_config_id=11,
        file_ref=saved["file_ref"],
        public_base_url="https://example.test",
    )
    path = prepared["transport"]["url"].split("/api", 1)[1]

    response = client.get(f"/api{path}")

    assert response.status_code == 200
    assert response.content == b"download-me"
    assert response.headers["x-heysure-transfer-id"] == prepared["transfer_id"]


def test_http_put_stream_is_the_wire_transport(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    prepared = member_transfer.prepare_upload(
        user_id=7,
        ai_config_id=11,
        file_name="payload.bin",
        public_base_url="https://example.test",
    )
    path = prepared["transport"]["url"].split("/api", 1)[1]
    response = client.put(f"/api{path}", content=b"streamed")
    assert response.status_code == 200
    assert response.json()["state"] == "uploaded"
