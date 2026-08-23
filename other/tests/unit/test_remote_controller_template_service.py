import json
from types import SimpleNamespace

import pytest
from sqlalchemy import Text, UniqueConstraint

from api.models import RemoteControllerTemplate
from api.services.remote_control.controller_schema import TemplateCreate, TemplateUpdate
from api.services.remote_control import controller_templates as service


def _create_body(template_id="custom-pad"):
    return TemplateCreate.model_validate({
        "schema": "remote_controller_template.v1",
        "id": template_id,
        "name": "Pad",
        "deviceTypes": ["desktop"],
        "requiredCapabilities": ["remote_control"],
        "layout": {"columns": 2, "gap": "sm"},
        "controls": [{
            "id": "ok", "kind": "button", "label": "OK",
            "action": {"type": "key", "key": "Enter"},
        }],
    })


def _update_body(revision, name="Updated"):
    payload = _create_body().model_dump(mode="json", by_alias=True)
    payload.pop("id")
    payload["name"] = name
    payload["expectedRevision"] = revision
    return TemplateUpdate.model_validate(payload)


class FakeSession:
    def __init__(self):
        self.added = []
        self.deleted = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        return None


def _patch_storage(monkeypatch, *, row=None, rows=()):
    monkeypatch.setattr(service, "_lock_user", lambda _session, _user_id: None)
    monkeypatch.setattr(service, "_owned_row", lambda *_args, **_kwargs: row)
    monkeypatch.setattr(service, "_user_rows", lambda *_args, **_kwargs: list(rows))


def test_create_custom_template_persists_canonical_content(monkeypatch):
    session = FakeSession()
    _patch_storage(monkeypatch)

    document = service.create_template(session, 7, _create_body())

    row = session.added[0]
    assert row.user_id == 7
    assert row.template_id == "custom-pad"
    assert row.revision == 1
    assert "expectedRevision" not in row.document_json
    assert "id" not in json.loads(row.document_json)
    assert document.id == "custom-pad"
    assert document.builtin is False


def test_create_rejects_builtin_id_and_per_user_limit(monkeypatch):
    session = FakeSession()
    _patch_storage(monkeypatch)
    with pytest.raises(service.TemplateConflictError):
        service.create_template(session, 7, _create_body("media"))

    rows = [SimpleNamespace(builtin_override=False) for _ in range(service.MAX_TEMPLATES_PER_USER)]
    _patch_storage(monkeypatch, rows=rows)
    with pytest.raises(service.TemplateLimitError):
        service.create_template(session, 7, _create_body())

    deleted = SimpleNamespace(deleted_at=123.0)
    monkeypatch.setattr(service, "_owned_row", lambda *_args, **_kwargs: deleted)
    with pytest.raises(service.TemplateLimitError):
        service.create_template(session, 7, _create_body())


def test_update_builtin_creates_user_override_at_next_revision(monkeypatch):
    session = FakeSession()
    _patch_storage(monkeypatch)

    document = service.update_template(session, 9, "media", _update_body(1))

    row = session.added[0]
    assert row.user_id == 9
    assert row.template_id == "media"
    assert row.builtin_override is True
    assert document.revision == 2
    assert document.builtin is True


def test_update_and_delete_require_matching_revision(monkeypatch):
    row = RemoteControllerTemplate(
        id="row-1", user_id=7, template_id="custom-pad",
        document_json=service._serialize(_create_body()), revision=3,
    )
    session = FakeSession()
    _patch_storage(monkeypatch, row=row)

    with pytest.raises(service.TemplateConflictError):
        service.update_template(session, 7, "custom-pad", _update_body(2))
    with pytest.raises(service.TemplateConflictError):
        service.delete_template(session, 7, "custom-pad", 2)

    revision = service.delete_template(session, 7, "custom-pad", 3)
    assert revision == 4
    assert row.deleted_at is not None


def test_restore_builtin_is_idempotent_and_keeps_revision_monotonic(monkeypatch):
    session = FakeSession()
    _patch_storage(monkeypatch)
    document = service.restore_builtin(session, 7, "media", 1)
    assert document.id == "media"
    assert document.revision == 1
    assert session.deleted == []

    override = RemoteControllerTemplate(
        id="row-2", user_id=7, template_id="media",
        document_json=service._serialize(_update_body(1)),
        revision=2, builtin_override=True,
    )
    _patch_storage(monkeypatch, row=override)
    document = service.restore_builtin(session, 7, "media", 2)
    assert document.revision == 3
    assert document.builtin is True
    assert session.deleted == []
    assert json.loads(override.document_json)["name"] == "媒体遥控器"

    document = service.restore_builtin(session, 7, "media", 3)
    assert document.revision == 3
    with pytest.raises(service.TemplateConflictError):
        service.restore_builtin(session, 7, "media", 2)


def test_list_overlays_only_current_users_rows(monkeypatch):
    custom = RemoteControllerTemplate(
        id="row-3", user_id=42, template_id="custom-pad",
        document_json=service._serialize(_create_body()), revision=1,
    )
    captured = {}

    def rows_for(_session, user_id, **_kwargs):
        captured["user_id"] = user_id
        return [custom]

    monkeypatch.setattr(service, "_user_rows", rows_for)
    documents = service.list_templates(FakeSession(), 42, device_type="desktop")

    assert captured["user_id"] == 42
    assert "custom-pad" in {item.id for item in documents}
    assert all("desktop" in item.device_types for item in documents)


def test_orm_contract_uses_text_and_per_user_unique_key():
    table = RemoteControllerTemplate.__table__
    assert isinstance(table.c.document_json.type, Text)
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("user_id", "template_id") in unique_columns
