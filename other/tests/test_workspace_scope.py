from types import SimpleNamespace

from api.services.storage import agent_instructions, workspace_scope


def _cfg(*, ai_role="digital_member", digital_member_role="member", name="beta"):
    return SimpleNamespace(
        id=21,
        user_id=7,
        name=name,
        ai_role=ai_role,
        digital_member_role=digital_member_role,
    )


def test_digital_society_manager_uses_user_workspace_root(tmp_path, monkeypatch):
    manager = _cfg(digital_member_role="manager", name="alpha")

    class _Session:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec(self, _query):
            return self

        def first(self):
            return manager

    monkeypatch.setattr(workspace_scope, "Session", _Session)
    monkeypatch.setattr(workspace_scope, "user_workspace_dir", lambda _user_id: str(tmp_path / "7"))

    assert workspace_scope.member_workspace_dir(7, 21) == str(tmp_path / "7")


def test_ordinary_member_keeps_named_workspace_subdirectory(tmp_path, monkeypatch):
    member = _cfg()

    class _Session:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def exec(self, _query):
            return self

        def first(self):
            return member

    monkeypatch.setattr(workspace_scope, "Session", _Session)
    monkeypatch.setattr(workspace_scope, "user_workspace_dir", lambda _user_id: str(tmp_path / "7"))

    assert workspace_scope.member_workspace_dir(7, 21) == str(tmp_path / "7" / "21-beta")


def test_manager_agent_instructions_are_bootstrapped_at_root(tmp_path, monkeypatch):
    manager = _cfg(digital_member_role="manager", name="alpha")
    user_root = tmp_path / "7"
    legacy = user_root / "21-阿尔法" / "agent.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# legacy manager instructions\n", encoding="utf-8")
    monkeypatch.setattr(agent_instructions, "user_workspace_dir", lambda _user_id: str(user_root))

    path = agent_instructions.ensure_agent_md(7, manager)

    assert path == str(user_root / "agent.md")
    assert (user_root / "agent.md").read_text(encoding="utf-8") == legacy.read_text(encoding="utf-8")
    assert agent_instructions.load_agent_md(7, manager) == "# legacy manager instructions"
