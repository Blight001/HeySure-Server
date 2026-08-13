from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool
from pydantic import ValidationError

import api.sio as sio_module
from api.auth import create_access_token
from api.core.settings import Settings
from api.models import ChatRun, User, UserUpdate
from gateway.routers import admin_user_routes, auth


def _memory_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine, tables=[User.__table__, ChatRun.__table__])
    return Session(engine)


def _token(user: User, *, version=None, include_version=True, user_id=None):
    data = {
        "sub": user.account,
        "user_id": user.id if user_id is None else user_id,
    }
    if include_version:
        data["auth_version"] = user.auth_version if version is None else version
    return create_access_token(data)


def test_current_user_requires_matching_auth_version_and_user_id():
    with _memory_session() as session:
        user = User(name="Secure", account="secure-user", hashed_password="hash")
        session.add(user)
        session.commit()
        session.refresh(user)

        assert auth.get_current_user(f"Bearer {_token(user)}", session).id == user.id

        rejected = [
            _token(user, version=user.auth_version - 1),
            _token(user, include_version=False),
            _token(user, user_id=user.id + 1),
        ]
        for token in rejected:
            with pytest.raises(HTTPException) as exc:
                auth.get_current_user(f"Bearer {token}", session)
            assert exc.value.status_code == 401


def test_socket_token_resolution_obeys_auth_version(monkeypatch):
    with _memory_session() as session:
        user = User(name="Socket", account="socket-user", hashed_password="hash")
        session.add(user)
        session.commit()
        session.refresh(user)
        token = _token(user)
        engine = session.get_bind()

    monkeypatch.setattr(sio_module, "engine", engine)
    assert sio_module.resolve_user_token(token) == (user.id, user.account)
    with Session(engine) as session:
        stored = session.get(User, user.id)
        stored.auth_version += 1
        session.add(stored)
        session.commit()
    assert sio_module.resolve_user_token(token) is None


def test_public_default_secrets_are_rejected():
    base = {
        "_env_file": None,
        "DATABASE_URL": "postgresql+psycopg://user:pass@db/app",
        "JWT_SECRET": "test-jwt-secret-at-least-thirty-two-characters",
    }
    with pytest.raises(ValidationError):
        Settings(**{**base, "JWT_SECRET": "heysure-ai-secret-key-change-this-in-production"})
    with pytest.raises(ValidationError):
        Settings(**{**base, "internal_token": "heysure-dev-internal-token-change-me"})


def test_logout_revokes_the_presented_token():
    with _memory_session() as session:
        user = User(name="Secure", account="logout-user", hashed_password="hash")
        session.add(user)
        session.commit()
        session.refresh(user)
        run = ChatRun(
            run_id="run-before-logout",
            user_id=user.id,
            ai_kind="assistant",
            session_id="default",
            session_name="Default",
            status="running",
        )
        session.add(run)
        session.commit()
        token = _token(user)

        assert auth.logout(f"Bearer {token}", session) is None
        session.refresh(run)
        assert run.status == "stopped"
        assert run.stop_requested is True
        with pytest.raises(HTTPException) as exc:
            auth.get_current_user(f"Bearer {token}", session)
        assert exc.value.status_code == 401


def test_admin_password_reset_revokes_existing_tokens(monkeypatch):
    monkeypatch.setattr(admin_user_routes, "get_password_hash", lambda value: f"hash:{value}")
    monkeypatch.setattr(admin_user_routes, "_record_audit", lambda *_args, **_kwargs: None)

    with _memory_session() as session:
        owner = User(name="Owner", account="owner", hashed_password="hash", role="owner")
        target = User(name="Target", account="target", hashed_password="hash")
        session.add(owner)
        session.add(target)
        session.commit()
        session.refresh(target)
        previous_version = target.auth_version

        result = admin_user_routes.reset_user_password(
            target.id,
            SimpleNamespace(new_password="new-password"),
            session,
            owner,
        )

        session.refresh(target)
        assert result == {"ok": True, "user_id": target.id}
        assert target.auth_version == previous_version + 1


def test_self_password_change_revokes_token_and_active_run(monkeypatch):
    disconnected = []
    monkeypatch.setattr(auth, "get_password_hash", lambda value: f"hash:{value}")
    monkeypatch.setattr(auth, "_user_payload", lambda user: user)
    monkeypatch.setattr(
        "api.socket_events.disconnect_user_sockets",
        lambda user_id: disconnected.append(user_id),
    )

    with _memory_session() as session:
        user = User(name="Self", account="self-user", hashed_password="hash")
        session.add(user)
        session.commit()
        session.refresh(user)
        run = ChatRun(
            run_id="run-before-password-change",
            user_id=user.id,
            ai_kind="assistant",
            session_id="default",
            session_name="Default",
            status="running",
        )
        session.add(run)
        session.commit()
        token = _token(user)

        auth.update_profile(
            UserUpdate(password="new-password"),
            f"Bearer {token}",
            session,
        )

        session.refresh(user)
        session.refresh(run)
        assert user.auth_version == 2
        assert run.status == "stopped"
        assert disconnected == [user.id]
        with pytest.raises(HTTPException):
            auth.get_current_user(f"Bearer {token}", session)
