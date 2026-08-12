import json
import uuid

import pytest
from sqlmodel import Session

from api.database import engine
from api.models import AssistantAIConfig, User, WorkflowCard
from tools.automation_access import _card_visible


pytestmark = pytest.mark.integration


def test_postgres_persists_selected_workflow_card_ai_scope():
    suffix = uuid.uuid4().hex
    with Session(engine) as session:
        user = User(name="workflow-access-test", account=f"workflow-access-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        allowed = AssistantAIConfig(user_id=user.id, name=f"allowed-{suffix}")
        denied = AssistantAIConfig(user_id=user.id, name=f"denied-{suffix}")
        session.add(allowed)
        session.add(denied)
        session.commit()
        session.refresh(allowed)
        session.refresh(denied)
        card = WorkflowCard(
            id=f"workflow-access-{suffix}",
            user_id=user.id,
            created_by=user.id,
            name="Selected access",
            status="active",
            access_scope="selected",
            allowed_ai_config_ids_json=json.dumps([allowed.id]),
        )
        session.add(card)
        session.commit()
        session.refresh(card)
        user_id, allowed_id, denied_id, card_id = user.id, allowed.id, denied.id, card.id

    try:
        with Session(engine) as session:
            persisted = session.get(WorkflowCard, card_id)
            assert persisted.access_scope == "selected"
            assert json.loads(persisted.allowed_ai_config_ids_json) == [allowed_id]
            assert _card_visible(persisted, allowed_id)
            assert not _card_visible(persisted, denied_id)
    finally:
        with Session(engine) as session:
            card = session.get(WorkflowCard, card_id)
            if card:
                session.delete(card)
            for config_id in (allowed_id, denied_id):
                config = session.get(AssistantAIConfig, config_id)
                if config:
                    session.delete(config)
            user = session.get(User, user_id)
            if user:
                session.delete(user)
            session.commit()
