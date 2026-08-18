import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.integrations.media_source import MediaSource
from connector_runtime.bots.qq import service


class _Response:
    def __init__(self, data, *, ok=True, text=""):
        self._data = data
        self.ok = ok
        self.text = text or str(data)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


class _HttpSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _source(path, *, url="https://cdn.example.test/video.mp4"):
    return MediaSource(
        path=str(path),
        filename="video.mp4",
        mime_type="video/mp4",
        source_url=url,
    )


def _patch_dependencies(monkeypatch, http):
    cfg = SimpleNamespace(user_id=3, id=9)
    monkeypatch.setattr(service, "_load_qq_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(service, "get_qq_access_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(service, "_qq_api_base", lambda _cfg: "https://qq.test")
    monkeypatch.setattr(service, "_qq_headers", lambda *_args: {"Authorization": "QQBot token"})
    monkeypatch.setattr(service, "_qq_http_session", lambda: http)


def test_qq_media_url_success_does_not_upload_file_data(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video-bytes")
    http = _HttpSession([_Response({"file_info": "url-file-info"})])
    _patch_dependencies(monkeypatch, http)

    result = service.upload_qq_media_file_info(
        3, 9, source=_source(media), target_id="openid", target_type="c2c", media_type="video"
    )

    assert result == "url-file-info"
    assert len(http.calls) == 1
    assert http.calls[0][1]["json"] == {
        "file_type": 2,
        "srv_send_msg": False,
        "url": "https://cdn.example.test/video.mp4",
    }


def test_qq_media_url_failure_falls_back_to_downloaded_bytes(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video-bytes")
    http = _HttpSession([
        _Response({"code": 40034025, "message": "fetch url failed"}, ok=False),
        _Response({"data": {"file_info": "bytes-file-info"}}),
    ])
    _patch_dependencies(monkeypatch, http)

    result = service.upload_qq_media_file_info(
        3, 9, source=_source(media), target_id="openid", target_type="c2c", media_type="video"
    )

    assert result == "bytes-file-info"
    assert len(http.calls) == 2
    fallback = http.calls[1][1]["json"]
    assert fallback["file_type"] == 2
    assert fallback["file_data"] == base64.b64encode(b"video-bytes").decode("ascii")
    assert "url" not in fallback


def test_qq_media_missing_file_info_also_falls_back(monkeypatch, tmp_path):
    media = tmp_path / "report.txt"
    media.write_bytes(b"report")
    http = _HttpSession([_Response({"code": 0}), _Response({"file_info": "fallback-info"})])
    _patch_dependencies(monkeypatch, http)

    result = service.upload_qq_media_file_info(
        3, 9, source=_source(media, url="https://example.test/report.txt"),
        target_id="group-id", target_type="group", media_type="file",
    )

    assert result == "fallback-info"
    assert http.calls[1][1]["json"]["file_type"] == 4


def test_qq_media_reports_both_url_and_fallback_errors(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video-bytes")
    http = _HttpSession([
        _Response({"code": 1, "message": "url rejected"}, ok=False),
        _Response({"code": 2, "message": "data rejected"}, ok=False),
    ])
    _patch_dependencies(monkeypatch, http)

    with pytest.raises(HTTPException) as caught:
        service.upload_qq_media_file_info(
            3, 9, source=_source(media), target_id="openid", target_type="c2c", media_type="video"
        )

    assert caught.value.status_code == 502
    assert "URL upload failed" in caught.value.detail
    assert "file_data fallback failed" in caught.value.detail
    assert "url rejected" in caught.value.detail
    assert "data rejected" in caught.value.detail
