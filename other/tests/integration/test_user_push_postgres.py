import time
import uuid

import pytest
from sqlmodel import Session

from api.database import engine
from api.models import User
from api.models.user_notification import UserNotification
from api.services.notifications.push_delivery import claim_notifications, complete_delivery


pytestmark = pytest.mark.integration


def test_postgres_push_lease_cannot_be_claimed_twice():
    suffix = uuid.uuid4().hex
    with Session(engine) as session:
        user = User(name="push-test", account=f"push-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        notice = UserNotification(
            id=f"notice_{suffix}",
            user_id=user.id,
            body="integration",
            app_push_required=True,
            push_status="pending",
            push_next_attempt_at=time.time() - 1,
        )
        session.add(notice)
        session.commit()
        user_id = user.id
        notice_id = notice.id

    try:
        with Session(engine) as first:
            claimed = claim_notifications(first, owner="connector-a")
            assert notice_id in {row.id for row in claimed}
        with Session(engine) as second:
            claimed_again = claim_notifications(second, owner="connector-b")
            assert notice_id not in {row.id for row in claimed_again}
            delivered = complete_delivery(
                second,
                notification_id=notice_id,
                owner="connector-a",
                delivered=True,
            )
            assert delivered and delivered.push_status == "delivered"
    finally:
        with Session(engine) as cleanup:
            stored_notice = cleanup.get(UserNotification, notice_id)
            stored_user = cleanup.get(User, user_id)
            if stored_notice:
                cleanup.delete(stored_notice)
                cleanup.commit()
            if stored_user:
                cleanup.delete(stored_user)
                cleanup.commit()
