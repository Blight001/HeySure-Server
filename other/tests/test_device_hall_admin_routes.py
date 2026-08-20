from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.database import get_session
from gateway.routers import device_hall_admin
from gateway.routers.admin import require_admin_user


def _client():
    app = FastAPI()
    app.include_router(device_hall_admin.router, prefix=device_hall_admin.PREFIX)
    app.dependency_overrides[require_admin_user] = lambda: SimpleNamespace(id=1, account="owner")
    app.dependency_overrides[get_session] = lambda: object()
    return TestClient(app)


def test_admin_catalog_requires_admin_dependency(monkeypatch):
    expected = {"products": [{"id": "windows-desktop", "targets": []}]}
    monkeypatch.setattr(device_hall_admin, "admin_catalog", lambda: expected)
    response = _client().get("/api/device-hall/admin/catalog")
    assert response.status_code == 200
    assert response.json() == expected


def test_admin_upload_contract_and_notification(monkeypatch):
    captured = {}

    def fake_publish(stream, **kwargs):
        kwargs["stream"] = stream
        captured.update(kwargs)
        return {"ok": True, "release": {"version": kwargs["request"].version}}

    async def fake_notify(payload):
        captured["notification"] = payload

    monkeypatch.setattr(device_hall_admin, "publish_release", fake_publish)
    monkeypatch.setattr(device_hall_admin, "_notify_release", fake_notify)
    monkeypatch.setattr(device_hall_admin, "_record_audit", lambda *_args, **_kwargs: None)
    response = _client().post(
        "/api/device-hall/admin/releases",
        data={
            "product_id": "windows-desktop",
            "target_id": "windows-x86_64-stable",
            "version": "1.2.0",
            "release_notes": "fixes",
            "mandatory": "true",
        },
        files={"file": ("setup.exe", b"installer", "application/octet-stream")},
    )
    assert response.status_code == 200
    assert captured["request"].filename == "setup.exe"
    assert captured["stream"].closed
    assert captured["request"].mandatory is True
    assert captured["notification"]["latest_version"] == "1.2.0"


def test_admin_withdraw_contract(monkeypatch):
    monkeypatch.setattr(
        device_hall_admin, "withdraw_release",
        lambda product, target, **kwargs: {"ok": True, "version": kwargs["version"]},
    )
    monkeypatch.setattr(device_hall_admin, "_record_audit", lambda *_args, **_kwargs: None)
    response = _client().delete(
        "/api/device-hall/admin/releases/windows-desktop/windows-x86_64-stable?version=1.2.0"
    )
    assert response.status_code == 200
    assert response.json()["version"] == "1.2.0"
