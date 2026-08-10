import json

from sqlmodel import Session, SQLModel, create_engine

from api.models import DevicePresence, User, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import delete_card, owned_card, publish_card


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[User.__table__, DevicePresence.__table__, WorkflowCard.__table__, WorkflowCardVersion.__table__],
    )
    return engine


def _user(session: Session) -> User:
    user = User(name="Test", account="workflow-card-delete", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_delete_card_soft_deletes_without_changing_release_status():
    with Session(_database()) as session:
        user = _user(session)
        card = WorkflowCard(
            id="card", user_id=user.id, created_by=user.id, name="Card", status="published"
        )
        session.add(card)
        session.commit()

        delete_card(session, card)

        session.refresh(card)
        assert card.deleted_at is not None
        assert card.status == "published"
        assert owned_card(session, user.id, card.id) is None
        assert session.get(WorkflowCard, card.id) is not None


def test_legacy_archived_card_is_treated_as_deleted():
    with Session(_database()) as session:
        user = _user(session)
        card = WorkflowCard(
            id="legacy", user_id=user.id, created_by=user.id, name="Legacy", status="archived"
        )
        session.add(card)
        session.commit()

        assert owned_card(session, user.id, card.id) is None


def test_publish_binds_multiple_devices_with_one_common_tool_contract(monkeypatch):
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4},
        "output": {},
    }
    with Session(_database()) as session:
        user = _user(session)
        card = WorkflowCard(
            id="multi", user_id=user.id, created_by=user.id, name="Multi",
            draft_definition_json=json.dumps(definition),
        )
        session.add(card)
        session.add(DevicePresence(user_id=user.id, device_id="one", device_type="desktop", online=True))
        session.add(DevicePresence(user_id=user.id, device_id="two", device_type="browser", online=True))
        session.commit()
        monkeypatch.setattr(
            "api.services.workflows.card_service.tool_defs_for_agent",
            lambda *_: {"demo": {"input_schema": schema, "destructive": False}},
        )

        version = publish_card(session, card, user.id, device_ids=["one", "two"])
        payload = json.loads(version.tool_contracts_json)

        assert json.loads(version.contract_device_ids_json) == ["one", "two"]
        assert payload["demo"]["publishedDeviceIds"] == ["one", "two"]
        assert payload["demo"]["providers"] == ["browser", "desktop"]
