import json

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from api.models import (
    AssistantAIConfig,
    BotSessionRoute,
    BotUserCursor,
    ChatMessage,
    ChatMessageMedia,
    ChatRun,
    ChatSession,
    TokenUsageSnapshot,
    User,
)
from api.services.model_presets import session_model_preset_entry
from connector_runtime.bots import commands
from connector_runtime.bots.qq import router as qq_router


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            AssistantAIConfig.__table__,
            ChatSession.__table__,
            ChatMessage.__table__,
            ChatRun.__table__,
            BotSessionRoute.__table__,
            BotUserCursor.__table__,
            ChatMessageMedia.__table__,
            TokenUsageSnapshot.__table__,
        ],
    )
    return engine


def _seed(session: Session):
    presets = [
        {
            "id": "fast",
            "name": "快速模型",
            "api_key": "key-fast",
            "base_url": "https://fast.invalid/v1/chat/completions",
            "model": "fast-model",
        },
        {
            "id": "smart",
            "name": "智能模型",
            "api_key": "key-smart",
            "base_url": "https://smart.invalid/v1/chat/completions",
            "model": "smart-model",
            "provider": "openai",
            "tool_protocol": "native",
        },
    ]
    user = User(
        name="owner",
        account="owner",
        hashed_password="hash",
        model_presets=json.dumps(presets),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    cfg = AssistantAIConfig(
        user_id=int(user.id),
        name="robot",
        model_preset_id="fast",
        bot_channel="qq",
    )
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    first = ChatSession(
        user_id=int(user.id),
        ai_config_id=int(cfg.id),
        ai_kind="core",
        session_id="qq_home",
        session_name="当前对话",
    )
    second = ChatSession(
        user_id=int(user.id),
        ai_config_id=int(cfg.id),
        ai_kind="core",
        session_id="session_old",
        session_name="历史对话",
    )
    session.add(first)
    session.add(second)
    session.commit()
    session.refresh(first)
    session.refresh(second)
    return user, cfg, first, second


def _handle(session, user, cfg, text):
    return commands.handle_bot_command(
        session,
        text=text,
        channel="qq",
        user=user,
        cfg=cfg,
        ai_kind="core",
        identity_key="openid",
        current_session_id="qq_home",
        current_session_name="当前对话",
        home_session_id="qq_home",
    )


def test_help_and_plain_message_detection():
    assert commands.parse_bot_command("普通消息") is None
    assert commands.parse_bot_command(" /models smart ") == ("models", "smart")
    engine = _database()
    with Session(engine) as session:
        user, cfg, _, _ = _seed(session)
        result = _handle(session, user, cfg, "/help")
        assert result.command == "help"
        for name in ("/list", "/change", "/delete", "/stop", "/clear", "/mcp", "/models", "/prompt"):
            assert name in result.text


def test_list_change_and_delete_conversation_by_numeric_id():
    engine = _database()
    with Session(engine) as session:
        user, cfg, first, second = _seed(session)
        session.add(ChatMessage(
            user_id=int(user.id),
            ai_config_id=int(cfg.id),
            ai_kind="core",
            session_id=second.session_id,
            session_name=second.session_name,
            role="user",
            content="old",
        ))
        session.commit()

        listed = _handle(session, user, cfg, "/list")
        assert f"[{first.id}] 当前对话" in listed.text
        assert f"[{second.id}] 历史对话" in listed.text

        changed = _handle(session, user, cfg, f"/change {second.id}")
        assert "已切换" in changed.text
        cursor = session.exec(select(BotUserCursor)).first()
        assert cursor.active_session_id == second.session_id

        deleted = _handle(session, user, cfg, f"/delete {second.id}")
        assert "已删除对话" in deleted.text
        assert session.get(ChatSession, second.id) is None
        session.refresh(cursor)
        assert cursor.active_session_id == "qq_home"


def test_stop_and_clear_current_conversation():
    engine = _database()
    with Session(engine) as session:
        user, cfg, first, _ = _seed(session)
        run = ChatRun(
            run_id="run-active",
            user_id=int(user.id),
            ai_config_id=int(cfg.id),
            ai_kind="core",
            session_id=first.session_id,
            status="running",
        )
        session.add(run)
        session.add(ChatMessage(
            user_id=int(user.id),
            ai_config_id=int(cfg.id),
            ai_kind="core",
            session_id=first.session_id,
            session_name=first.session_name,
            role="user",
            content="hello",
        ))
        session.commit()

        stopped = _handle(session, user, cfg, "/stop")
        assert "停止 1 个运行" in stopped.text
        session.refresh(run)
        assert run.stop_requested is True
        assert run.status == "stopped"

        cleared = _handle(session, user, cfg, "/clear")
        assert "删除 1 条消息" in cleared.text
        assert session.exec(select(ChatMessage)).all() == []
        assert session.get(ChatSession, first.id) is not None


def test_models_switch_is_scoped_to_current_session():
    engine = _database()
    with Session(engine) as session:
        user, cfg, first, second = _seed(session)
        listed = _handle(session, user, cfg, "/models")
        assert "[fast] 快速模型" in listed.text
        assert "当前对话" in listed.text

        switched = _handle(session, user, cfg, "/models smart")
        assert "[smart] 智能模型" in switched.text
        session.refresh(first)
        session.refresh(second)
        assert first.model_preset_id == "smart"
        assert second.model_preset_id == ""
        selected = session_model_preset_entry(session, user, cfg, first.session_id, "core")
        assert selected["id"] == "smart"
        assert selected["provider"] == "openai"


def test_mcp_and_prompt_commands_use_current_runtime(monkeypatch):
    engine = _database()
    with Session(engine) as session:
        user, cfg, _, _ = _seed(session)
        monkeypatch.setattr(
            commands,
            "_resolve_ai_runtime",
            lambda *args, **kwargs: (cfg, "key", "url", "model", "base prompt"),
        )
        monkeypatch.setattr(
            commands,
            "build_runtime_system_prompt_and_tools",
            lambda *args, **kwargs: ("effective prompt", {"tool.b", "tool.a"}),
        )

        mcp = _handle(session, user, cfg, "/mcp")
        assert mcp.text.endswith("1. tool.a\n2. tool.b")
        prompt = _handle(session, user, cfg, "/prompt")
        assert prompt.text == "当前对话 Prompt：\n\neffective prompt"


def test_qq_command_reply_is_chunked_with_ordered_sequences(monkeypatch):
    calls = []
    monkeypatch.setattr(
        qq_router,
        "_send_qq_text",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    sent = qq_router._send_qq_command_text(
        user_id=1,
        ai_config_id=2,
        target_id="openid",
        target_type="c2c",
        text="x" * 3700,
        msg_id="source",
    )

    assert sent == 3
    assert [call["msg_seq"] for call in calls] == [1, 2, 3]
    assert [len(call["text"]) for call in calls] == [1800, 1800, 100]
