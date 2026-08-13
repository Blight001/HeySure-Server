import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from connector_runtime.bots import get as get_bot
from connector_runtime.bots.qq import login
from connector_runtime.bots.qq import qr_protocol


def test_qq_adapter_supports_official_qr_login():
    bot = get_bot("qq")
    assert bot is not None
    assert callable(bot.start_login)
    assert callable(bot.login_status)
    assert callable(bot.logout)


def test_decrypt_app_secret_matches_tencent_envelope():
    key = bytes(range(32))
    iv = bytes(range(12))
    encrypted_and_tag = AESGCM(key).encrypt(iv, b"app-secret-value", None)
    encrypted = base64.b64encode(iv + encrypted_and_tag).decode("ascii")

    assert qr_protocol.decrypt_app_secret(
        encrypted, base64.b64encode(key).decode("ascii")
    ) == "app-secret-value"


def test_create_bind_task_uses_ephemeral_key(monkeypatch):
    captured = {}

    def fake_post(path, payload):
        captured.update(path=path, payload=payload)
        return {"task_id": "task-123"}

    monkeypatch.setattr(qr_protocol, "_post_bind", fake_post)
    task_id, key = qr_protocol.create_bind_task()

    assert task_id == "task-123"
    assert captured["path"] == "/lite/create_bind_task"
    assert captured["payload"] == {"key": key}
    assert len(base64.b64decode(key)) == 32


def test_qr_url_targets_exact_official_host():
    url = qr_protocol.build_qr_url("task id")
    assert url.startswith("https://q.qq.com/qqbot/openclaw/connect.html?")
    assert "task_id=task+id" in url
    assert "source=heysure" in url


def test_completed_poll_persists_before_marking_connected(monkeypatch):
    manager = login.QQLoginManager()
    attempt = login.QQLoginAttempt(
        "session", 3, 7, "conn_qq", "task", "key", "https://q.qq.com/qr"
    )
    manager._attempts[attempt.connection_ref] = attempt
    saved = []
    refreshed = []
    monkeypatch.setattr(login, "poll_bind_task", lambda _task: {"status": 2})
    monkeypatch.setattr(manager, "_save_connection", lambda current, response: saved.append((current, response)))
    monkeypatch.setattr(manager, "_refresh_connections", lambda: refreshed.append(True))

    manager._poll(attempt)

    assert saved == [(attempt, {"status": 2})]
    assert refreshed == [True]
    assert manager.snapshot(3, "conn_qq")["connected"] is True


def test_failed_completed_poll_is_terminal(monkeypatch):
    manager = login.QQLoginManager()
    attempt = login.QQLoginAttempt(
        "session", 3, 7, "conn_qq", "task", "key", "https://q.qq.com/qr"
    )
    manager._attempts[attempt.connection_ref] = attempt
    monkeypatch.setattr(login, "poll_bind_task", lambda _task: {"status": 2})
    monkeypatch.setattr(manager, "_save_connection", lambda *_args: (_ for _ in ()).throw(ValueError("已绑定")))

    manager._poll(attempt)

    status = manager.snapshot(3, "conn_qq")
    assert status["state"] == "failed"
    assert status["message"] == "已绑定"
