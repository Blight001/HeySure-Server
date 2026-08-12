from types import SimpleNamespace

from starlette.requests import Request

from gateway.routers import device_file_routes


def _request(headers=None):
    values = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({"type": "http", "scheme": "http", "server": ("internal", 3000), "path": "/", "headers": values})


def test_authenticated_device_link_issue_uses_forwarded_public_base(monkeypatch):
    monkeypatch.setattr(device_file_routes.settings, "public_base_url", "")
    monkeypatch.setattr(
        device_file_routes,
        "get_current_user",
        lambda authorization, session: SimpleNamespace(id=7),
    )
    calls = []
    monkeypatch.setattr(
        device_file_routes,
        "create_temporary_file_link",
        lambda **kwargs: calls.append(kwargs) or {"file_ref": kwargs["file_ref"], "url": "https://public/link"},
    )
    payload = device_file_routes.DeviceFileLinkRequest(
        ai_config_id=19,
        file_refs=["file_" + "a" * 32],
    )
    result = device_file_routes.create_device_file_links(
        payload,
        _request({"x-forwarded-host": "files.example", "x-forwarded-proto": "https"}),
        session=object(),
        authorization="Bearer test",
    )

    assert result["count"] == 1
    assert calls[0]["user_id"] == 7
    assert calls[0]["ai_config_id"] == 19
    assert calls[0]["public_base_url"] == "https://files.example"


def test_public_download_returns_integrity_headers(tmp_path, monkeypatch):
    source = tmp_path / "报告.txt"
    source.write_bytes(b"download")
    monkeypatch.setattr(
        device_file_routes,
        "resolve_temporary_file_link",
        lambda grant_id, token: {
            "server_path": str(source),
            "file_name": source.name,
            "mime_type": "text/plain",
            "file_ref": "file_" + "a" * 32,
            "sha256": "f" * 64,
            "expires_at": 1300,
        },
    )
    response = device_file_routes.download_temporary_file("fgrant_" + "b" * 32, "token")
    assert response.path == str(source)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-heysure-file-sha256"] == "f" * 64
    assert response.headers["x-heysure-file-name"] == "%E6%8A%A5%E5%91%8A.txt"
