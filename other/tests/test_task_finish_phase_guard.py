import pytest
from fastapi import HTTPException

from tools import task_plan as task_plan_tools


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakePlan:
    plan_id = "plan_test"
    job_id = "job_test"


def test_plan_finish_blocks_when_phase_is_unclosed(monkeypatch):
    finish_called = False

    def fake_finish_plan(*args, **kwargs):
        nonlocal finish_called
        finish_called = True

    monkeypatch.setattr(task_plan_tools, "Session", lambda engine: _FakeSession())
    monkeypatch.setattr(task_plan_tools.plan_service, "get_active_plan", lambda *args, **kwargs: _FakePlan())
    monkeypatch.setattr(task_plan_tools.plan_service, "unfinished_phases", lambda *args, **kwargs: [{
        "seq": 0,
        "title": "验证产出",
        "goal": "确认阶段目标达成",
        "status": "active",
    }])
    monkeypatch.setattr(task_plan_tools.plan_service, "finish_plan", fake_finish_plan)

    with pytest.raises(HTTPException) as exc:
        task_plan_tools._plan_finish(
            user_id=1,
            args={"outcome": "failure", "summary": "执行失败，准备收尾。"},
            ai_config_id=2,
        )

    assert exc.value.status_code == 409
    assert "阶段性目标尚未全部收尾" in str(exc.value.detail)
    assert "阶段1" in str(exc.value.detail)
    assert finish_called is False
