import uuid

import pytest
from sqlmodel import Session, select

from api.database import engine
from api.models import RemoteControllerTemplate, User
from api.services.remote_control.controller_schema import TemplateCreate, TemplateUpdate
from api.services.remote_control.controller_templates import (
    TemplateConflictError,
    TemplateNotFoundError,
    create_template,
    get_template,
    list_templates,
    update_template,
)


pytestmark = pytest.mark.integration


def _body(template_id: str, name: str) -> TemplateCreate:
    return TemplateCreate.model_validate({
        "schema": "remote_controller_template.v1",
        "id": template_id,
        "name": name,
        "deviceTypes": ["desktop"],
        "requiredCapabilities": ["remote_control"],
        "layout": {"columns": 1, "gap": "sm"},
        "controls": [{
            "id": "ok", "kind": "button", "label": "OK",
            "action": {"type": "key", "key": "Enter"},
        }],
    })


def _update(revision: int, name: str) -> TemplateUpdate:
    payload = _body("ignored", name).model_dump(mode="json", by_alias=True)
    payload.pop("id")
    payload["expectedRevision"] = revision
    return TemplateUpdate.model_validate(payload)


def test_postgres_enforces_user_isolation_and_revision_lock():
    suffix = uuid.uuid4().hex
    template_id = f"pad-{suffix[:12]}"
    private_template_id = f"private-{suffix[:12]}"
    user_ids = []
    try:
        with Session(engine) as session:
            for index in range(2):
                user = User(
                    name=f"RCT test {index}",
                    account=f"rct-test-{index}-{suffix}",
                    hashed_password="not-used",
                )
                session.add(user)
                session.commit()
                session.refresh(user)
                user_ids.append(user.id)
        with Session(engine) as session:
            first = create_template(session, user_ids[0], _body(template_id, "First user"))
            create_template(session, user_ids[0], _body(private_template_id, "Private"))
        with Session(engine) as session:
            second = create_template(session, user_ids[1], _body(template_id, "Second user"))
        assert first.id == second.id == template_id

        with Session(engine) as session:
            first_items = list_templates(session, user_ids[0])
            second_items = list_templates(session, user_ids[1])
        assert next(item.name for item in first_items if item.id == template_id) == "First user"
        assert next(item.name for item in second_items if item.id == template_id) == "Second user"
        assert private_template_id not in {item.id for item in second_items}
        with Session(engine) as session, pytest.raises(TemplateNotFoundError):
            get_template(session, user_ids[1], private_template_id)

        with Session(engine) as session:
            updated = update_template(session, user_ids[0], template_id, _update(1, "Updated"))
        assert updated.revision == 2
        with Session(engine) as session, pytest.raises(TemplateConflictError):
            update_template(session, user_ids[0], template_id, _update(1, "Stale"))
    finally:
        with Session(engine) as session:
            if user_ids:
                rows = session.exec(
                    select(RemoteControllerTemplate).where(
                        RemoteControllerTemplate.user_id.in_(user_ids)
                    )
                ).all()
                for row in rows:
                    session.delete(row)
                users = session.exec(select(User).where(User.id.in_(user_ids))).all()
                for user in users:
                    session.delete(user)
                session.commit()
