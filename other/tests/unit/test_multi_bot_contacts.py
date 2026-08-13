import json

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api.models import AssistantAIConfig, BotConnection, BotSessionRoute, ChatSession
from api.services.bot_directory import (
    config_view_for_connection,
    connection_config,
    ensure_connection,
    readable_connection_config,
    release_connection_binding,
    update_connection_config,
)
from connector_runtime.bots.qq._config import QQ_DEFAULTS
from connector_runtime.bots.registry import iter_active_for_config, iter_bots, sync_connection_directory
from gateway.routers.bots import (
    BotConnectionUpdateRequest,
    _connection_config_update,
    _sync_legacy_channel_enabled,
)
from connector_runtime.bots.session_cursor import list_ai_sessions
from api.runtime import run_context
from tools.conversation import _conversation_base_scope


def test_all_enabled_channels_are_active_independently():
    cfg = AssistantAIConfig(user_id=1, name="multi", bot_channel="feishu")
    cfg.bot_configs = json.dumps({
        "feishu": {"enabled": True, "app_id": "a", "app_secret": "b"},
        "qq": {"enabled": True, "app_id": "c", "app_secret": "d"},
        "wechat": {"enabled": True},
    })
    assert {bot.channel for bot in iter_active_for_config(cfg)} == {"feishu", "qq", "wechat"}
    assert all(bot.is_enabled(cfg) for bot in iter_bots())


def test_external_contact_session_listing_is_isolated():
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(db, tables=[ChatSession.__table__, BotSessionRoute.__table__])
    with Session(db) as session:
        session.add(ChatSession(
            user_id=1, ai_config_id=2, ai_kind="core", session_id="a", session_name="A", bot_contact_id=10
        ))
        session.add(ChatSession(
            user_id=1, ai_config_id=2, ai_kind="core", session_id="b", session_name="B", bot_contact_id=20
        ))
        session.commit()
        rows = list_ai_sessions(
            session,
            user_id=1,
            ai_config_id=2,
            ai_kind="core",
            bot_contact_id=10,
        )
    assert [item["session_id"] for item in rows] == ["a"]


def test_bot_run_cannot_override_authoritative_conversation_scope():
    token = run_context.set_run_session_context({
        "session_id": "contact-session",
        "ai_config_id": 2,
        "ai_kind": "core",
        "bot_contact_id": 10,
    })
    try:
        scope = _conversation_base_scope(
            {"session_id": "other-user-session", "ai_config_id": 999, "ai_kind": "assistant"},
            999,
        )
    finally:
        run_context.reset_run_session_context(token)
    assert scope == {"session_id": "contact-session", "ai_config_id": 2, "ai_kind": "core"}


def test_same_ai_channel_can_hold_independent_account_instances():
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(db, tables=[BotConnection.__table__])
    with Session(db) as session:
        first = ensure_connection(
            session, user_id=1, ai_config_id=2, channel="qq", name="QQ A", create_new=True
        )
        update_connection_config(first, {"enabled": True, "app_id": "a", "app_secret": "secret-a"}, QQ_DEFAULTS)
        second = ensure_connection(
            session, user_id=1, ai_config_id=2, channel="qq", name="QQ B", create_new=True
        )
        update_connection_config(second, {"enabled": True, "app_id": "b", "app_secret": "secret-b"}, QQ_DEFAULTS)
        session.commit()
        rows = session.exec(select(BotConnection).order_by(BotConnection.id)).all()
        configs = [connection_config(row, QQ_DEFAULTS) for row in rows]
    assert len({row.connection_ref for row in rows}) == 2
    assert [item["app_id"] for item in configs] == ["a", "b"]
    assert [item["app_secret"] for item in configs] == ["secret-a", "secret-b"]


def test_legacy_ai_save_does_not_overwrite_connection_directory_credentials():
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(db, tables=[AssistantAIConfig.__table__, BotConnection.__table__])
    with Session(db) as session:
        cfg = AssistantAIConfig(
            user_id=1,
            name="multi",
            bot_configs=json.dumps({
                "qq": {"enabled": True, "app_id": "stale", "app_secret": "stale-secret"},
            }),
        )
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
        row = ensure_connection(
            session,
            user_id=1,
            ai_config_id=int(cfg.id),
            channel="qq",
            name="QQ",
            create_new=True,
        )
        update_connection_config(
            row,
            {"enabled": True, "app_id": "current", "app_secret": "current-secret"},
            QQ_DEFAULTS,
        )
        session.commit()

        sync_connection_directory(session, cfg, preserve_existing=True)

        session.refresh(row)
        saved = connection_config(row, QQ_DEFAULTS)
    assert saved["app_id"] == "current"
    assert saved["app_secret"] == "current-secret"


def test_connection_directory_projects_channel_enabled_without_credentials():
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(db, tables=[AssistantAIConfig.__table__, BotConnection.__table__])
    with Session(db) as session:
        cfg = AssistantAIConfig(
            user_id=1,
            name="multi",
            bot_configs=json.dumps({"qq": {"enabled": False}}),
        )
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
        row = ensure_connection(
            session, user_id=1, ai_config_id=int(cfg.id), channel="qq", create_new=True
        )
        update_connection_config(
            row,
            {"enabled": True, "app_id": "directory-only", "app_secret": "directory-secret"},
            QQ_DEFAULTS,
        )
        _sync_legacy_channel_enabled(session, cfg, "qq")
        session.commit()
        projected = json.loads(cfg.bot_configs)["qq"]
    assert projected == {"enabled": True}


def test_top_level_enabled_wins_over_stale_nested_config():
    body = BotConnectionUpdateRequest(
        enabled=True,
        config={"enabled": False, "app_id": "app", "app_secret": "secret"},
    )
    assert _connection_config_update(body) == {
        "enabled": True,
        "app_id": "app",
        "app_secret": "secret",
    }


def test_connection_config_view_has_independent_orm_state():
    cfg = AssistantAIConfig(user_id=1, name="multi")
    cfg.bot_configs = json.dumps({"qq": {"enabled": False}, "feishu": {"enabled": True}})
    row = BotConnection(
        connection_ref="conn-qq",
        user_id=1,
        ai_config_id=2,
        channel="qq",
    )
    update_connection_config(
        row,
        {"enabled": True, "app_id": "app", "app_secret": "secret"},
        QQ_DEFAULTS,
    )

    view = config_view_for_connection(cfg, row, QQ_DEFAULTS)

    assert view is not cfg
    assert json.loads(view.bot_configs)["qq"]["enabled"] is True
    assert json.loads(cfg.bot_configs)["qq"]["enabled"] is False
    # Regression: assigning a scoped channel view must not emit an ORM event
    # through the original SQLModel instance's weak parent reference.
    view.bot_configs = view.bot_configs


def test_deleted_connection_releases_provider_identity_and_credentials():
    row = BotConnection(
        connection_ref="conn-old",
        user_id=1,
        ai_config_id=2,
        channel="wechat",
        provider_account_id="wx-account",
        owner_external_id="wx-user",
        base_url="https://ilinkai.weixin.qq.com",
        credentials_encrypted="encrypted",
        sync_cursor="cursor",
        state="connected",
        is_default=True,
    )
    release_connection_binding(row, deleted=True)
    assert row.state == "deleted"
    assert row.provider_account_id == ""
    assert row.owner_external_id == ""
    assert row.credentials_encrypted == ""
    assert row.sync_cursor == ""
    assert row.enabled is False
    assert row.is_default is False


def test_unreadable_connection_can_be_repaired_only_with_fresh_secret(monkeypatch):
    row = BotConnection(
        connection_ref="conn-old-key",
        user_id=1,
        ai_config_id=2,
        channel="qq",
        credentials_encrypted="fernet:v1:old-key",
        enabled=True,
    )
    monkeypatch.setattr(
        "api.services.bot_directory.decrypt_credentials",
        lambda _value: (_ for _ in ()).throw(ValueError("old key")),
    )
    try:
        update_connection_config(row, {"app_id": "new-app"}, QQ_DEFAULTS)
    except ValueError as exc:
        assert "require re-entry" in str(exc)
    else:
        raise AssertionError("an autosave without a fresh secret must be rejected")

    monkeypatch.setattr(
        "api.services.bot_directory.encrypt_credentials",
        lambda value: json.dumps(value),
    )
    update_connection_config(
        row,
        {"enabled": True, "app_id": "new-app", "app_secret": "fresh-secret"},
        QQ_DEFAULTS,
    )
    assert "fresh-secret" in row.credentials_encrypted


def test_unreadable_connection_isolated_from_peer_account(monkeypatch):
    row = BotConnection(
        connection_ref="conn-bad",
        user_id=1,
        ai_config_id=2,
        channel="qq",
        credentials_encrypted="fernet:v1:old-key",
        enabled=True,
    )
    monkeypatch.setattr(
        "api.services.bot_directory.decrypt_credentials",
        lambda _value: (_ for _ in ()).throw(ValueError("old key")),
    )

    config, error = readable_connection_config(row, QQ_DEFAULTS)

    assert config is None
    assert "重新填写" in error
