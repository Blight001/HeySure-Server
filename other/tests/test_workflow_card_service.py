import json

from sqlmodel import Session, SQLModel, create_engine, select

from api.models import AssistantAIConfig, DevicePresence, User, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import create_card, delete_card, owned_card, update_card
from api.services.workflows.schemas import CardCreate, CardUpdate
from api.services.workflows.ai_interaction import _run_device_id
from api.services.workflows.step_runtime import step_device_id
from api.models import WorkflowRun


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[
            User.__table__, AssistantAIConfig.__table__, DevicePresence.__table__,
            WorkflowCard.__table__, WorkflowCardVersion.__table__,
        ],
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


def test_card_selected_access_scope_persists_valid_ai_members():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "finish",
        "steps": {"finish": {"type": "end"}},
        "limits": {"timeoutSeconds": 30, "maxTransitions": 3},
        "output": {},
    }
    with Session(_database()) as session:
        user = _user(session)
        member = AssistantAIConfig(user_id=user.id, name="Allowed")
        session.add(member)
        session.commit()
        session.refresh(member)

        card = create_card(session, user.id, CardCreate(
            name="Selected",
            definition=definition,
            access_scope="selected",
            allowed_ai_config_ids=[member.id],
        ))

        assert card.access_scope == "selected"
        assert json.loads(card.allowed_ai_config_ids_json) == [member.id]


def test_save_creates_immutable_versions_and_binds_contract_devices(monkeypatch):
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "steps": {
            "call": {
                "type": "mcp", "toolRef": {"namespace": "device", "deviceId": "one", "name": "demo"},
                "arguments": {}, "saveAs": "demo", "next": "call_two",
            },
            "call_two": {
                "type": "mcp", "toolRef": {"namespace": "device", "deviceId": "two", "name": "other"},
                "arguments": {}, "saveAs": "other", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4},
        "output": {},
    }
    with Session(_database()) as session:
        user = _user(session)
        session.add(DevicePresence(user_id=user.id, device_id="one", device_type="desktop", online=True))
        session.add(DevicePresence(user_id=user.id, device_id="two", device_type="browser", online=True))
        session.commit()
        monkeypatch.setattr(
            "api.services.workflows.card_service.tool_defs_for_agent",
            lambda _user_id, device_id: {
                "demo" if device_id == "one" else "other": {
                    "input_schema": schema, "destructive": False,
                },
            },
        )

        card = create_card(session, user.id, CardCreate(
            name="Multi",
            definition=definition,
        ))
        version = session.get(WorkflowCardVersion, card.latest_version_id)
        payload = json.loads(version.tool_contracts_json)

        assert card.status == "active"
        assert version.version_number == 1
        assert json.loads(version.contract_device_ids_json) == ["one", "two"]
        assert payload["call"]["deviceId"] == "one"
        assert payload["call"]["publishedDeviceIds"] == ["one"]
        assert payload["call"]["providers"] == ["desktop"]
        assert payload["call_two"]["deviceId"] == "two"
        assert payload["call_two"]["providers"] == ["browser"]

        update_card(
            session,
            card,
            CardUpdate(description="saved again", definition=definition),
            user_id=user.id,
        )
        latest = session.get(WorkflowCardVersion, card.latest_version_id)
        assert latest.version_number == 2
        assert len(session.exec(
            select(WorkflowCardVersion).where(WorkflowCardVersion.card_id == card.id)
        ).all()) == 2


def test_device_mcp_card_requires_a_device_on_each_node_or_one_fallback():
    definition = {
        "schemaVersion": 1,
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "steps": {
            "call": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "name": "demo", "schemaDigest": "known"},
                "arguments": {},
                "saveAs": "demo_result",
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 3},
        "output": {},
    }
    with Session(_database()) as session:
        user = _user(session)
        try:
            create_card(session, user.id, CardCreate(name="Missing device", definition=definition))
        except Exception as exc:
            assert "toolRef.deviceId" in str(exc)
        else:
            raise AssertionError("device MCP cards must declare contract devices")


def test_run_uses_first_saved_contract_device_when_start_omits_device():
    version = WorkflowCardVersion(
        id="version", card_id="card", version_number=1,
        definition_json="{}", definition_digest="digest",
        contract_device_ids_json='["desktop-one","browser-two"]', published_by=1,
    )
    assert _run_device_id(version, "") == "desktop-one"
    assert _run_device_id(version, "explicit") == "explicit"


def test_run_prefers_explicit_default_device_and_overrides_default_bound_steps():
    version = WorkflowCardVersion(
        id="version", card_id="card", version_number=1,
        definition_json='{"defaultDeviceId":"desktop-default"}', definition_digest="digest",
        contract_device_ids_json='["desktop-default","desktop-test"]', published_by=1,
    )
    run = WorkflowRun(
        id="run", card_id="card", card_version_id="version", user_id=1,
        device_id="desktop-test", deadline_at=999, idempotency_key="key",
        variables_json='{"_device_override":{"from":"desktop-default","to":"desktop-test"}}',
    )

    assert _run_device_id(version, "") == "desktop-default"
    assert step_device_id({"toolRef": {"deviceId": "desktop-default"}}, run) == "desktop-test"
    assert step_device_id({"toolRef": {"deviceId": "other-terminal"}}, run) == "other-terminal"
