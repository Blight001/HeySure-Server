from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from api.services.remote_control.controller_schema import TemplateDocument, TemplateUpdate
from api.services.remote_control.controller_templates import _builtin_document
from gateway.routers import remote_controller_templates as routes


class DummySession:
    pass


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix=routes.PREFIX)
    app.dependency_overrides[routes.get_session] = lambda: DummySession()
    monkeypatch.setattr(routes, "get_current_user", lambda *_args: SimpleNamespace(id=3))
    return TestClient(app)


def _update_body(revision=1):
    return TemplateUpdate.model_validate({
        "schema": "remote_controller_template.v1",
        "expectedRevision": revision,
        "name": "Updated",
        "deviceTypes": ["desktop"],
        "requiredCapabilities": ["remote_control"],
        "layout": {"columns": 1, "gap": "sm"},
        "controls": [{
            "id": "ok", "kind": "button", "label": "OK",
            "action": {"type": "key", "key": "Enter"},
        }],
    })


def test_list_uses_authenticated_user_scope(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes, "get_current_user", lambda token, session: SimpleNamespace(id=44))

    def fake_list(_session, user_id, **filters):
        captured.update(user_id=user_id, **filters)
        return [_builtin_document("direction")]

    monkeypatch.setattr(routes, "list_templates", fake_list)
    response = Response()
    payload = routes.list_remote_controller_templates(
        response,
        device_type="desktop",
        capability="remote_control",
        session=DummySession(),
        authorization="Bearer user-token",
    )

    assert captured == {"user_id": 44, "device_type": "desktop", "capability": "remote_control"}
    assert payload["total"] == 1
    assert payload["items"][0]["deviceTypes"]
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_get_sets_revision_etag(monkeypatch):
    monkeypatch.setattr(routes, "get_current_user", lambda *_args: SimpleNamespace(id=3))
    monkeypatch.setattr(routes, "get_template", lambda *_args: _builtin_document("media"))
    response = Response()

    payload = routes.get_remote_controller_template(
        "media", response, session=DummySession(), authorization="Bearer token"
    )

    assert payload["revision"] == 1
    assert response.headers["ETag"] == 'W/"rct-media-1"'
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"


def test_update_maps_revision_conflict_to_409(monkeypatch):
    monkeypatch.setattr(routes, "get_current_user", lambda *_args: SimpleNamespace(id=3))

    def conflict(*_args):
        raise routes.TemplateConflictError("conflict")

    monkeypatch.setattr(routes, "update_template", conflict)
    with pytest.raises(HTTPException) as raised:
        routes.update_remote_controller_template(
            "media", _update_body(), Response(), session=DummySession(), authorization="Bearer token"
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "TEMPLATE_REVISION_CONFLICT"


def test_schema_endpoint_requires_auth_and_exposes_realtime_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "get_current_user", lambda token, session: calls.append(token))
    payload = routes.get_template_schema(session=DummySession(), authorization="Bearer token")
    assert calls == ["Bearer token"]
    assert payload["schema"] == "remote_controller_template.v1"
    assert "controllerAction" in payload["schemas"]


def _create_json():
    return {
        "schema": "remote_controller_template.v1",
        "id": "custom-pad",
        "name": "Pad",
        "deviceTypes": ["desktop"],
        "requiredCapabilities": ["remote_control"],
        "layout": {"columns": 1, "gap": "sm"},
        "controls": [{
            "id": "ok", "kind": "button", "label": "OK",
            "action": {"type": "key", "key": "Enter"},
        }],
    }


def test_real_http_create_contract_rejects_response_only_fields(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "create_template",
        lambda _session, _user_id, body: TemplateDocument(
            **body.model_dump(mode="json", by_alias=True), revision=1, builtin=False
        ),
    )
    created = client.post(routes.PREFIX, json=_create_json(), headers={"Authorization": "Bearer token"})
    assert created.status_code == 201
    assert created.json()["id"] == "custom-pad"

    invalid = {**_create_json(), "revision": 1, "builtin": False}
    rejected = client.post(routes.PREFIX, json=invalid, headers={"Authorization": "Bearer token"})
    assert rejected.status_code == 422


def test_real_http_mutation_revision_shapes_are_strict(client, monkeypatch):
    monkeypatch.setattr(routes, "update_template", lambda *_args: _builtin_document("media"))
    update = _create_json()
    update.pop("id")
    update["expectedRevision"] = 1
    assert client.put(f"{routes.PREFIX}/media", json=update).status_code == 200
    wrong_alias = dict(update)
    wrong_alias["expected_revision"] = wrong_alias.pop("expectedRevision")
    assert client.put(f"{routes.PREFIX}/media", json=wrong_alias).status_code == 422

    monkeypatch.setattr(routes, "delete_template", lambda *_args: 2)
    assert client.delete(f"{routes.PREFIX}/custom-pad?expectedRevision=1").status_code == 200
    assert client.request(
        "DELETE", f"{routes.PREFIX}/custom-pad", json={"expectedRevision": 1}
    ).status_code == 422

    monkeypatch.setattr(routes, "restore_builtin", lambda *_args: _builtin_document("media"))
    assert client.post(
        f"{routes.PREFIX}/media/restore", json={"expectedRevision": 1}
    ).status_code == 200
    assert client.post(f"{routes.PREFIX}/media/restore").status_code == 422
