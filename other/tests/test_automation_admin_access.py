from types import SimpleNamespace

from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

import api.database as database
import tools.automation as automation
from api.models import AssistantAIConfig, User, WorkflowCard, WorkflowCardVersion
from api.services.mcp.mcp_prompt_groups import automation_card_catalog_text
from tools.automation_access import _admin_actor


MINIMAL_DEFINITION = {
    "schemaVersion": 1,
    "inputSchema": {"type": "object"},
    "startStepId": "finish",
    "steps": {"finish": {"type": "end"}},
    "limits": {"timeoutSeconds": 30, "maxTransitions": 3},
    "output": {},
}


def test_private_card_catalog_survives_a_new_chat_without_leaking(monkeypatch):
    memory = create_engine("sqlite://")
    SQLModel.metadata.create_all(
        memory,
        tables=[
            User.__table__, AssistantAIConfig.__table__,
            WorkflowCard.__table__, WorkflowCardVersion.__table__,
        ],
    )
    with Session(memory) as session:
        user = User(name="Test", account="automation-cross-chat", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        owner = AssistantAIConfig(user_id=user.id, name="owner", ai_role="digital_member")
        other = AssistantAIConfig(user_id=user.id, name="other", ai_role="digital_member")
        selected = AssistantAIConfig(user_id=user.id, name="selected", ai_role="digital_member")
        session.add(owner)
        session.add(other)
        session.add(selected)
        session.commit()
        session.refresh(owner)
        session.refresh(other)
        session.refresh(selected)
        cards = [
            WorkflowCard(
                id="owner-card", user_id=user.id, created_by=user.id, name="Owner",
                status="active", tags_json=f'["ai_owner:{owner.id}"]', access_scope="owner",
            ),
            WorkflowCard(
                id="other-card", user_id=user.id, created_by=user.id, name="Other",
                status="active", tags_json=f'["ai_owner:{other.id}"]', access_scope="selected",
                allowed_ai_config_ids_json=f"[{selected.id}]",
            ),
            WorkflowCard(
                id="public-card", user_id=user.id, created_by=user.id, name="Public",
                status="active", tags_json="[]",
            ),
        ]
        session.add_all(cards)
        session.commit()
        user_id, owner_id, selected_id = int(user.id), int(owner.id), int(selected.id)

    monkeypatch.setattr(database, "engine", memory)
    owner_catalog = automation_card_catalog_text(user_id, owner_id)
    assert "owner-card" in owner_catalog
    assert "public-card" in owner_catalog
    assert "other-card" not in owner_catalog
    assert "other-card" in automation_card_catalog_text(user_id, selected_id)
