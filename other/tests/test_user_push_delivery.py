from sqlmodel import Session, SQLModel, create_engine

from api.models import User
from api.models.user_notification import UserNotification
from api.models.user_push_endpoint import UserPushEndpoint
from api.services.notifications.push_delivery import (
    active_endpoints,
    claim_notifications,
    complete_delivery,
    endpoint_metadata,
    release_without_endpoint,
    upsert_endpoint,
)


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__,
        UserNotification.__table__,
        UserPushEndpoint.__table__,
    ])
    return engine


def _user(session: Session, account: str = "owner") -> User:
    user = User(name=account, account=account, hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_endpoint_upsert_is_user_owned_and_never_returns_token():
    engine = _database()
    with Session(engine) as session:
        first = _user(session, "first")
        second = _user(session, "second")
        created = upsert_endpoint(
            session,
            user_id=first.id,
            provider="huawei",
            device_id="android-1",
            push_token="secret-token-one",
            app_version="2.2.0",
        )
        replaced = upsert_endpoint(
            session,
            user_id=second.id,
            provider="huawei",
            device_id="android-1",
            push_token="secret-token-two",
        )
        assert replaced.id == created.id
        assert replaced.user_id == second.id
        assert active_endpoints(session, user_id=first.id, provider="huawei") == []
        assert active_endpoints(session, user_id=second.id, provider="huawei") == [replaced]
        assert "token" not in endpoint_metadata(replaced)


def test_push_lease_success_and_missing_endpoint_release():
    engine = _database()
    with Session(engine) as session:
        user = _user(session)
        item = UserNotification(
            id="notice-1",
            user_id=user.id,
            body="完成",
            app_push_required=True,
            push_status="pending",
            push_next_attempt_at=1.0,
            created_at=1.0,
            updated_at=1.0,
        )
        session.add(item)
        session.commit()

        claimed = claim_notifications(session, owner="worker-a", now=2.0)
        assert [row.id for row in claimed] == [item.id]
        assert claimed[0].push_attempts == 1

        released = release_without_endpoint(session, notification_id=item.id, owner="worker-a")
        assert released and released.push_status == "pending"
        assert released.push_attempts == 0

        released.push_next_attempt_at = 1.0
        session.add(released)
        session.commit()
        claim_notifications(session, owner="worker-b", now=2.0)
        completed = complete_delivery(
            session,
            notification_id=item.id,
            owner="worker-b",
            delivered=True,
        )
        assert completed and completed.push_status == "delivered"
        assert completed.push_delivered_at is not None


def test_failed_push_retries_then_becomes_terminal():
    engine = _database()
    with Session(engine) as session:
        user = _user(session)
        item = UserNotification(
            id="notice-2",
            user_id=user.id,
            app_push_required=True,
            push_status="pending",
            push_next_attempt_at=1.0,
            created_at=1.0,
            updated_at=1.0,
        )
        session.add(item)
        session.commit()
        for attempt in range(1, 6):
            item.push_next_attempt_at = 1.0
            session.add(item)
            session.commit()
            claim_notifications(session, owner="worker", now=2.0)
            item = complete_delivery(
                session,
                notification_id=item.id,
                owner="worker",
                delivered=False,
                error_code="hms_unavailable",
            )
            assert item is not None
            expected = "failed" if attempt == 5 else "retry"
            assert item.push_status == expected
        assert item.push_attempts == 5
        assert item.push_last_error_code == "hms_unavailable"
