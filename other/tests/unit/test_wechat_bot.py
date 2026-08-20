from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api.models import BotSessionRoute
from api.services.bot_credentials import decrypt_credentials, encrypt_credentials
from connector_runtime.bots import get as get_bot
from connector_runtime.bots.wechat.ilink_client import ILinkClient, _safe_base_url
from connector_runtime.bots.wechat.login import (
    LoginAttempt,
    WeChatLoginManager,
    _credentials_with_token,
)
from connector_runtime.bots.wechat.media import (
    _aes_decrypt,
    _parse_aes_key,
    _safe_cdn_url,
    _media_kind,
    _message_item,
    download_items,
    send_media,
)
from connector_runtime.bots.wechat.router import _message_text, _parse_incoming
from connector_runtime.bots.wechat.routes_store import load_wechat_route, register_wechat_route
from connector_runtime.bots.wechat.adapter import split_wechat_text
from connector_runtime.bots import notify as bot_notify
from connector_runtime.bots.wechat import service as wechat_service


def test_wechat_adapter_is_registered():
    bot = get_bot("wechat")
    assert bot is not None
    assert bot.label == "微信"
    assert bot.session_prefix == "wechat_"


def test_bot_credentials_round_trip():
    encrypted = encrypt_credentials({"bot_token": "secret-value"})
    assert encrypted.startswith("fernet:v1:")
    assert "secret-value" not in encrypted
    assert decrypt_credentials(encrypted) == {"bot_token": "secret-value"}


def test_local_tokens_ignore_deleted_or_disabled_connections(monkeypatch):
    manager = WeChatLoginManager()
    rows = [
        SimpleNamespace(enabled=True, state="connected", credentials_encrypted=encrypt_credentials({"bot_token": "active"})),
        SimpleNamespace(enabled=False, state="disconnected", credentials_encrypted=encrypt_credentials({"bot_token": "disabled"})),
        SimpleNamespace(enabled=False, state="deleted", credentials_encrypted=encrypt_credentials({"bot_token": "deleted"})),
    ]

    class FakeResult:
        def all(self):
            return [row for row in rows if row.enabled and row.state != "deleted"]

    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec(self, _stmt):
            return FakeResult()

    monkeypatch.setattr("connector_runtime.bots.wechat.login.Session", FakeSession)
    assert manager._local_tokens() == ["active"]


@pytest.mark.parametrize("url", [
    "http://ilinkai.weixin.qq.com",
    "https://example.com",
    "https://weixin.qq.com.example.com",
])
def test_ilink_rejects_untrusted_base_urls(url):
    with pytest.raises(ValueError):
        _safe_base_url(url)


def test_ilink_send_text_uses_protocol_headers_and_base_info(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"ret": 0},
        )

    monkeypatch.setattr("connector_runtime.bots.wechat.ilink_client.requests.post", fake_post)
    result = ILinkClient(token="token-value").send_text(
        to_user_id="peer",
        context_token="context",
        text="hello",
    )
    assert result == {"ret": 0}
    assert captured["headers"]["Authorization"] == "Bearer token-value"
    assert captured["headers"]["AuthorizationType"] == "ilink_bot_token"
    msg = captured["json"]["msg"]
    assert msg["context_token"] == "context"
    assert msg["from_user_id"] == ""
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2
    assert msg["client_id"].startswith("heysure-wechat-")
    assert captured["json"]["base_info"]["bot_agent"] == "HeySureAI/2.0.0"


def test_forward_bot_falls_back_to_enabled_channel_with_recipient():
    class FakeBot:
        def __init__(self, channel, enabled, ready):
            self.channel = channel
            self.label = channel
            self._enabled = enabled
            self._ready = ready

        def is_enabled(self, _cfg):
            return self._enabled

        def has_default_recipient(self, _cfg):
            return self._ready

    feishu = FakeBot("feishu", False, False)
    wechat = FakeBot("wechat", True, True)

    bot, warning = bot_notify._forward_bot(
        SimpleNamespace(bot_channel="feishu"),
        [feishu, wechat],
    )

    assert bot is wechat
    assert warning is None


def test_wechat_send_retry_reuses_client_id(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def send_text(self, **kwargs):
            calls.append(kwargs)
            return {"ret": 7 if len(calls) == 1 else 0}

    monkeypatch.setattr(wechat_service, "ILinkClient", FakeClient)
    monkeypatch.setattr(
        wechat_service,
        "_connection",
        lambda *_args, **_kwargs: (SimpleNamespace(base_url="https://ilinkai.weixin.qq.com"), "token"),
    )
    monkeypatch.setattr(wechat_service.time, "sleep", lambda _seconds: None)

    result = wechat_service.send_wechat_text(
        1,
        2,
        text="hello",
        to_user_id="peer",
        context_token="context",
    )

    assert result == {"success": True}
    assert len(calls) == 2
    assert calls[0]["client_id"] == calls[1]["client_id"]


def test_wechat_send_failure_exposes_only_provider_ret(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def send_text(self, **_kwargs):
            return {"ret": 11, "sensitive": "must-not-leak"}

    monkeypatch.setattr(wechat_service, "ILinkClient", FakeClient)
    monkeypatch.setattr(
        wechat_service,
        "_connection",
        lambda *_args, **_kwargs: (SimpleNamespace(base_url="https://ilinkai.weixin.qq.com"), "token"),
    )
    monkeypatch.setattr(wechat_service.time, "sleep", lambda _seconds: None)

    with pytest.raises(Exception) as caught:
        wechat_service.send_wechat_text(
            1,
            2,
            text="hello",
            to_user_id="peer",
            context_token="context",
        )

    assert "ret=11" in str(caught.value)
    assert "must-not-leak" not in str(caught.value)


def test_wechat_send_retries_transient_http_error(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def send_text(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                response = SimpleNamespace(status_code=503)
                raise wechat_service.requests.HTTPError(response=response)
            return {"ret": 0}

    monkeypatch.setattr(wechat_service, "ILinkClient", FakeClient)
    monkeypatch.setattr(
        wechat_service,
        "_connection",
        lambda *_args, **_kwargs: (SimpleNamespace(base_url="https://ilinkai.weixin.qq.com"), "token"),
    )
    monkeypatch.setattr(wechat_service.time, "sleep", lambda _seconds: None)

    result = wechat_service.send_wechat_text(
        1,
        2,
        text="hello",
        to_user_id="peer",
        context_token="context",
    )

    assert result == {"success": True}
    assert len(calls) == 2
    assert calls[0]["client_id"] == calls[1]["client_id"]


def test_wechat_text_chunks_prefer_visible_boundaries():
    text = "第一段。\n\n" + ("甲" * 30) + "\n\n第二段。" + ("乙" * 20)
    chunks = split_wechat_text(text, limit=24)

    assert all(len(chunk) <= 24 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert chunks[0] == "第一段。"


def test_message_text_accepts_text_and_voice_transcript_only():
    assert _message_text({"item_list": [
        {"type": 1, "text_item": {"text": "第一段"}},
        {"type": 2, "image_item": {"url": "ignored"}},
        {"type": 3, "voice_item": {"text": "语音文字"}},
    ]}) == "第一段\n语音文字"


def test_media_only_private_message_is_accepted():
    incoming = _parse_incoming({
        "message_type": 1,
        "from_user_id": "peer",
        "context_token": "context",
        "message_id": 123,
        "item_list": [{"type": 2, "image_item": {"media": {}}}],
    })
    assert incoming is not None
    assert incoming.text == ""
    assert incoming.items[0]["type"] == 2


def test_wechat_media_upload_encrypts_and_builds_image_item(tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    clear = b"\x89PNG\r\n\x1a\n" + b"payload"
    source.write_bytes(clear)
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"x-encrypted-param": "download-param"}

        def raise_for_status(self):
            return None

    def fake_post(url, *, data, headers, timeout, allow_redirects):
        captured["ciphertext"] = data
        return FakeResponse()

    class FakeClient:
        def get_upload_url(self, body):
            captured["upload_body"] = body
            return {"ret": 0, "upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?id=redacted"}

        def send_text(self, **kwargs):
            return {"ret": 0}

        def send_item(self, **kwargs):
            captured["item"] = kwargs["item"]
            return {"ret": 0}

    monkeypatch.setattr("connector_runtime.bots.wechat.media_transport.requests.post", fake_post)
    result = send_media(
        FakeClient(), to_user_id="peer", context_token="context", text="说明",
        path=str(source), url="", media_type="image", file_name="capture.png",
    )
    assert result["success"] is True
    assert captured["upload_body"]["media_type"] == 1
    assert captured["ciphertext"] != clear
    media = captured["item"]["image_item"]["media"]
    key = _parse_aes_key(media["aes_key"])
    assert _aes_decrypt(captured["ciphertext"], key) == clear


def test_wechat_inbound_media_download_decrypts(monkeypatch):
    key = b"0123456789abcdef"
    from connector_runtime.bots.wechat.media import _aes_encrypt
    ciphertext = _aes_encrypt(b"%PDF-1.7\nbody", key)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            return [ciphertext]

    monkeypatch.setattr(
        "connector_runtime.bots.wechat.media_transport.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    records = download_items([{
        "type": 4,
        "file_item": {
            "file_name": "report.pdf",
            "media": {
                "full_url": "https://novac2c.cdn.weixin.qq.com/c2c/download?id=redacted",
                "aes_key": __import__("base64").b64encode(key.hex().encode()).decode(),
            },
        },
    }])
    assert records[0]["data"] == b"%PDF-1.7\nbody"
    assert records[0]["file_name"] == "report.pdf"
    assert records[0]["mime_type"] == "application/pdf"


def test_wechat_plain_inbound_image_compatibility(monkeypatch):
    clear = b"\x89PNG\r\n\x1a\nplain"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            return [clear]

    monkeypatch.setattr(
        "connector_runtime.bots.wechat.media_transport.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    records = download_items([{
        "type": 2,
        "image_item": {"media": {"full_url": "https://novac2c.cdn.weixin.qq.com/c2c/plain"}},
    }])
    assert records[0]["data"] == clear
    assert records[0]["mime_type"] == "image/png"


def test_wechat_structured_items_cover_file_video_and_audio_fallback():
    uploaded = {
        "download_param": "download-param",
        "aes_key": "YWVzLWtleQ==",
        "raw_size": 12,
        "cipher_size": 16,
    }
    file_item = _message_item("file", uploaded, "report.pdf")
    video_item = _message_item("video", uploaded, "clip.mp4")
    assert file_item["type"] == 4
    assert file_item["file_item"]["file_name"] == "report.pdf"
    assert video_item["type"] == 5
    assert video_item["video_item"]["video_size"] == 16
    assert _media_kind("audio/mpeg", "audio") == "file"


@pytest.mark.parametrize("url", [
    "http://novac2c.cdn.weixin.qq.com/c2c/upload",
    "https://qq.com.example.org/upload",
    "https://127.0.0.1/upload",
])
def test_wechat_rejects_untrusted_cdn_urls(url):
    with pytest.raises(ValueError):
        _safe_cdn_url(url)


@pytest.mark.parametrize(("provider_state", "expected_state", "done"), [
    ("scaned", "scanned", False),
    ("need_verifycode", "need_verifycode", False),
    ("verify_code_blocked", "failed", True),
    ("expired", "expired", True),
])
def test_login_state_transitions(provider_state, expected_state, done):
    manager = WeChatLoginManager()
    attempt = LoginAttempt("key", 7, 9, "qr", "https://example.invalid/qr")
    manager._attempts[7] = attempt
    finished, _ = manager._apply_poll_response(attempt, {"status": provider_state}, ILinkClient())
    assert finished is done
    assert manager.snapshot(7)["state"] == expected_state


def test_login_confirmed_persists_before_marking_connected(monkeypatch):
    manager = WeChatLoginManager()
    attempt = LoginAttempt("key", 7, 9, "qr", "https://example.invalid/qr")
    manager._attempts[7] = attempt
    saved = []
    monkeypatch.setattr(manager, "_save_connection", lambda current, response: saved.append((current, response)))
    finished, _ = manager._apply_poll_response(
        attempt,
        {"status": "confirmed", "bot_token": "token", "ilink_bot_id": "bot"},
        ILinkClient(),
    )
    assert finished is True
    assert saved and saved[0][0] is attempt
    assert manager.snapshot(7)["connected"] is True


def test_login_replaces_unreadable_credentials_for_disconnected_connection():
    encrypted = _credentials_with_token("fernet:v1:unreadable", "disconnected", "new-token")
    assert decrypt_credentials(encrypted) == {"bot_token": "new-token"}


def test_login_preserves_unreadable_credentials_for_connected_connection():
    with pytest.raises(ValueError, match="cannot be decrypted"):
        _credentials_with_token("fernet:v1:unreadable", "connected", "new-token")


def test_replaced_login_attempt_cannot_update_current_state():
    manager = WeChatLoginManager()
    old = LoginAttempt("old", 7, 9, "qr-old", "https://example.invalid/old")
    current = LoginAttempt("new", 7, 9, "qr-new", "https://example.invalid/new")
    manager._attempts[7] = current
    manager._set_attempt(old, "connected", "stale")
    assert manager.snapshot(7)["session_key"] == "new"
    assert manager.snapshot(7)["state"] == "awaiting_scan"


def test_wechat_route_encrypts_context_token():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine, tables=[BotSessionRoute.__table__])
    with Session(engine) as session:
        register_wechat_route(
            session,
            user_id=1,
            ai_config_id=2,
            ai_kind="core",
            session_id="wechat_2_peer",
            to_user_id="peer",
            context_token="context-secret",
        )
        row = session.exec(select(BotSessionRoute)).first()
        assert row is not None
        assert "context-secret" not in row.target_json
        route = load_wechat_route(session, SimpleNamespace(
            user_id=1,
            ai_config_id=2,
            ai_kind="core",
            session_id="wechat_2_peer",
        ))
        assert route is not None
        assert route.to_user_id == "peer"
        assert route.context_token == "context-secret"
