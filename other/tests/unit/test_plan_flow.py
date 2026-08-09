from types import SimpleNamespace

from ai_runtime.inference import plan_flow


def test_build_plan_completion_summary_reports_failed_phase():
    outcome, summary = plan_flow.build_plan_completion_summary(
        {
            "phases": [
                {"seq": 0, "title": "准备", "status": "completed", "summary": "完成"},
                {"seq": 1, "title": "验证", "status": "failed", "summary": "超时"},
            ]
        }
    )

    assert outcome == "failure"
    assert "- 准备：完成" in summary
    assert "- 验证：超时" in summary


def test_append_plan_directive_adds_overview_then_active_phase(monkeypatch):
    progress = {
        "phase_count": 2,
        "phases": [{"seq": 0}, {"seq": 1}],
    }
    monkeypatch.setattr(plan_flow.plan_service, "plan_progress", lambda *_: progress)
    monkeypatch.setattr(plan_flow.phase_context, "render_plan_overview", lambda _: "overview")
    monkeypatch.setattr(
        plan_flow.phase_context,
        "render_phase_directive",
        lambda phase, count: f"phase={phase['seq']}/{count}",
    )
    conversation = []

    plan_flow.append_plan_directive(
        conversation,
        object(),
        SimpleNamespace(current_phase_seq=1, goal="goal"),
        awaiting_finish=False,
    )

    assert conversation == [
        {"role": "user", "content": "overview"},
        {"role": "user", "content": "phase=1/2"},
    ]


def test_append_plan_directive_uses_finish_notice(monkeypatch):
    monkeypatch.setattr(
        plan_flow.plan_service,
        "plan_progress",
        lambda *_: {"phase_count": 1, "phases": [{"seq": 0}]},
    )
    monkeypatch.setattr(plan_flow.phase_context, "render_plan_overview", lambda _: "overview")
    monkeypatch.setattr(
        plan_flow.phase_context,
        "render_finish_required_notice",
        lambda goal: f"finish:{goal}",
    )
    conversation = []

    plan_flow.append_plan_directive(
        conversation,
        object(),
        SimpleNamespace(current_phase_seq=0, goal="ship"),
        awaiting_finish=True,
    )

    assert conversation[-1] == {"role": "user", "content": "finish:ship"}
