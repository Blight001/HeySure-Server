import json

from sqlmodel import Session, SQLModel, create_engine

from api.models import AssistantAIConfig, User
from api.models.user_notification import UserNotification
from api.services.notifications.user_notifications import (
    create_notification,
    list_notifications,
    mark_all_read,
    mark_read,
    notification_events_since,
    pending_device_notifications,
)


def _database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__,
        AssistantAIConfig.__table__,
        UserNotification.__table__,
    ])
    return engine


def _seed(session: Session):
    user = User(name="Owner", account="owner", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    agent = AssistantAIConfig(user_id=user.id, name="贝塔")
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return user, agent


def test_fallback_notification_is_durable_and_device_payload_is_safe():
    engine = _database()
    with Session(engine) as session:
        user, agent = _seed(session)
        item = create_notification(
            session,
            user_id=user.id,
            ai_config_id=agent.id,
            body="任务完成，请查收",
            attachments=[{
                "file_ref": "file_" + "a" * 32,
                "file_name": "报告.pdf",
                "mime_type": "application/pdf",
                "bytes": 42,
                "server_path": "/secret/workspace/report.pdf",
            }],
            app_push_required=True,
            external_channel="qq",
            external_delivered=False,
        )

        inbox = list_notifications(session, user_id=user.id, unread_only=True)
        device = pending_device_notifications(session, user_id=user.id)
        assert inbox[0]["notification_id"] == item.id
        assert inbox[0]["title"] == "贝塔发来消息"
        assert inbox[0]["attachments"][0]["file_name"] == "报告.pdf"
        assert "server_path" not in json.dumps(inbox, ensure_ascii=False)
        assert "attachments" not in device[0]
        assert device[0]["attachment_count"] == 1
        assert item.push_status == "pending"


def test_read_transitions_cancel_device_notification_and_are_idempotent():
    engine = _database()
    with Session(engine) as session:
        user, agent = _seed(session)
        first = create_notification(
            session, user_id=user.id, ai_config_id=agent.id, body="一",
            app_push_required=True,
        )
        second = create_notification(
            session, user_id=user.id, ai_config_id=agent.id, body="二",
            app_push_required=True,
        )
        since = min(first.created_at, second.created_at) - 1
        created, read = notification_events_since(session, since=since)
        assert {row["notification_id"] for row in created} == {first.id, second.id}
        assert read == []

        updated = mark_read(session, user_id=user.id, notification_id=first.id)
        assert updated and updated.status == "read"
        assert updated.push_status == "cancelled"
        same = mark_read(session, user_id=user.id, notification_id=first.id)
        assert same and same.read_at == updated.read_at
        assert [row["notification_id"] for row in pending_device_notifications(session, user_id=user.id)] == [second.id]

        _, read = notification_events_since(session, since=since)
        assert any(row["notification_id"] == first.id for row in read)
        assert mark_all_read(session, user_id=user.id) == 1
        session.refresh(second)
        assert second.push_status == "cancelled"
        assert mark_all_read(session, user_id=user.id) == 0
        assert pending_device_notifications(session, user_id=user.id) == []


def test_other_user_cannot_mark_notification_read():
    engine = _database()
    with Session(engine) as session:
        user, agent = _seed(session)
        item = create_notification(
            session, user_id=user.id, ai_config_id=agent.id, body="私有消息",
            app_push_required=True,
        )
        assert mark_read(session, user_id=user.id + 1, notification_id=item.id) is None
        session.refresh(item)
        assert item.status == "unread"
