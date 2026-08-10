from sqlmodel import Session, SQLModel, create_engine

from api.models import User, WorkflowCard, WorkflowCardVersion
from api.services.workflows.card_service import delete_card, owned_card


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[User.__table__, WorkflowCard.__table__, WorkflowCardVersion.__table__],
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
