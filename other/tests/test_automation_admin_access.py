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


def _select_manager_card_for_admin(memory, cards, ids):
    admin_card, member_card, manager_card = cards
    admin_id, member_id, user_id = ids
    assert admin_card["tags"] == []
    assert admin_card["access_scope"] == "all"
    assert f"ai_owner:{member_id}" in member_card["tags"]
    assert member_card["access_scope"] == "owner"
    assert manager_card["tags"] == []
    assert manager_card["access_scope"] == "all"
    with Session(memory) as session:
        selected = session.get(WorkflowCard, manager_card["id"])
        selected.access_scope = "selected"
        selected.allowed_ai_config_ids_json = f"[{admin_id}]"
        session.add(selected)
        session.commit()
    admin_items = automation._list_cards(user_id, {"limit": 100}, admin_id)["items"]
    admin_item_ids = {item["id"] for item in admin_items}
    assert {admin_card["id"], manager_card["id"]} <= admin_item_ids
    assert member_card["id"] not in admin_item_ids
    return manager_card


def test_admin_and_assistant_admin_card_access_policy():
    memory = create_engine("sqlite://")
    SQLModel.metadata.create_all(
        memory,
        tables=[
            User.__table__,
            AssistantAIConfig.__table__,
            WorkflowCard.__table__,
            WorkflowCardVersion.__table__,
        ],
    )
    with Session(memory) as session:
        user = User(name="Test", account="automation-admin-access", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        assistant_admin = AssistantAIConfig(
            user_id=user.id,
            name="assistant-admin",
            ai_role="assistant_admin",
        )
        member = AssistantAIConfig(
            user_id=user.id,
            name="member",
            ai_role="digital_member",
            digital_member_role="member",
        )
        manager = AssistantAIConfig(
            user_id=user.id,
            name="manager",
            ai_role="digital_member",
            digital_member_role="manager",
        )
        session.add(assistant_admin)
        session.add(member)
        session.add(manager)
        session.commit()
        session.refresh(assistant_admin)
        session.refresh(member)
        session.refresh(manager)
        user_id = int(user.id)
        admin_id = int(assistant_admin.id)
        member_id = int(member.id)
        manager_id = int(manager.id)

    original_engine = automation.engine
    original_create = automation.create_validated_run
    original_payload = automation.run_payload
    automation.engine = memory
    try:
        admin_card = automation._create_card(
            user_id,
            {"action": "create", "name": "admin-public", "definition": MINIMAL_DEFINITION},
            admin_id,
        )
        member_card = automation._create_card(
            user_id,
            {"action": "create", "name": "member-private", "definition": MINIMAL_DEFINITION},
            member_id,
        )
        manager_card = automation._create_card(
            user_id,
            {"action": "create", "name": "manager-private", "definition": MINIMAL_DEFINITION},
            manager_id,
        )
        manager_card = _select_manager_card_for_admin(
            memory,
            (admin_card, member_card, manager_card),
            (admin_id, member_id, user_id),
        )
        member_items = automation._list_cards(user_id, {"limit": 100}, member_id)["items"]
        assert manager_card["id"] not in {item["id"] for item in member_items}

        with Session(memory) as session:
            assert _admin_actor(session, user_id, admin_id)
            assert not _admin_actor(session, user_id, member_id)
            assert automation._accessible_card(
                session,
                user_id,
                manager_card["id"],
                admin_id,
                admin_read=True,
            )
            assert automation._accessible_card(session, user_id, manager_card["id"], admin_id)

        automation.create_validated_run = lambda session, **kwargs: SimpleNamespace(id="run-ok")
        automation.run_payload = lambda row: {"id": row.id}
        assert automation._start_run(
            user_id,
            {"card_id": manager_card["id"]},
            admin_id,
        ) == {"id": "run-ok"}
        try:
            automation._start_run(user_id, {"card_id": manager_card["id"]}, member_id)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("member executed another AI card")

        cloned = automation._manage_card(
            user_id,
            {"action": "clone", "card_id": manager_card["id"]},
            admin_id,
        )
        assert not any(str(tag).startswith("ai_owner:") for tag in cloned["tags"])
    finally:
        automation.engine = original_engine
        automation.create_validated_run = original_create
        automation.run_payload = original_payload


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
        admin = AssistantAIConfig(user_id=user.id, name="admin", ai_role="assistant_admin")
        session.add(owner)
        session.add(other)
        session.add(admin)
        session.commit()
        session.refresh(owner)
        session.refresh(other)
        session.refresh(admin)
        cards = [
            WorkflowCard(
                id="owner-card", user_id=user.id, created_by=user.id, name="Owner",
                status="active", tags_json=f'["ai_owner:{owner.id}"]', access_scope="owner",
            ),
            WorkflowCard(
                id="other-card", user_id=user.id, created_by=user.id, name="Other",
                status="active", tags_json=f'["ai_owner:{other.id}"]', access_scope="selected",
                allowed_ai_config_ids_json=f"[{admin.id}]",
            ),
            WorkflowCard(
                id="public-card", user_id=user.id, created_by=user.id, name="Public",
                status="active", tags_json="[]",
            ),
        ]
        session.add_all(cards)
        session.commit()
        user_id, owner_id, admin_id = int(user.id), int(owner.id), int(admin.id)

    monkeypatch.setattr(database, "engine", memory)
    owner_catalog = automation_card_catalog_text(user_id, owner_id)
    assert "owner-card" in owner_catalog
    assert "public-card" in owner_catalog
    assert "other-card" not in owner_catalog
    assert "other-card" in automation_card_catalog_text(user_id, admin_id)
