import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session

from api.database import engine
from api.models import User
from api.services.access.session_security import revoke_user_sessions


pytestmark = pytest.mark.integration


def test_concurrent_revocations_increment_auth_version_without_lost_update():
    suffix = uuid.uuid4().hex
    with Session(engine) as session:
        user = User(name="auth-revocation", account=f"auth-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = int(user.id)

    def revoke_once():
        with Session(engine) as session:
            stored = session.get(User, user_id)
            revoke_user_sessions(session, stored)
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _index: revoke_once(), range(2)))
        with Session(engine) as session:
            assert session.get(User, user_id).auth_version == 3
    finally:
        with Session(engine) as session:
            stored = session.get(User, user_id)
            if stored:
                session.delete(stored)
                session.commit()
