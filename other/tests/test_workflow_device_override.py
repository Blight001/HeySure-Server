import json

from sqlmodel import Session, select

from api.models import DevicePresence, WorkflowStepRun
from api.services.workflows.ai_interaction import create_validated_run
from api.services.workflows.compiler import schema_digest
from api.services.workflows.run_service import advance_run
from api.services.workflows.step_runtime import step_run_device_id
from test_workflow_run_service import _database, _seed


def test_explicit_selected_device_overrides_only_default_bound_nodes():
    digest = schema_digest({})
    definition = {
        "schemaVersion": 1,
        "defaultDeviceId": "device",
        "inputSchema": {"type": "object"},
        "startStepId": "call",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 4},
        "steps": {
            "call": {
                "type": "mcp",
                "toolRef": {
                    "namespace": "device", "name": "demo", "deviceId": "device",
                    "provider": "desktop", "schemaDigest": digest,
                },
                "arguments": {}, "saveAs": "demo", "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {},
    }
    engine = _database()
    with Session(engine) as session:
        user, card = _seed(
            session,
            definition,
            tool_contracts={
                "call": {"provider": "desktop", "providers": ["desktop"], "schemaDigest": digest},
            },
            contract_device_ids=["device", "other"],
        )
        session.add(DevicePresence(
            user_id=user.id, device_id="other", device_type="desktop", online=True,
            tool_defs_json=json.dumps({"demo": {"input_schema": {}}}),
        ))
        session.commit()

        run = create_validated_run(
            session, user_id=user.id, card_id=card.id, device_id="other", input_value={},
        )
        advance_run(session, run.id)
        step = session.exec(select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)).one()

        assert run.device_id == "other"
        assert step_run_device_id(session, step) == "other"
