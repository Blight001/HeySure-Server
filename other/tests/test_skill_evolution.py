"""Automatic plan-to-Skill evolution tests."""

from unittest.mock import patch

from api.services.knowledge.skill_evolution import evolve_skill_from_plan


def test_successful_plan_creates_immediately_active_ai_skill():
    with patch("api.services.knowledge.librarian_core._find_thought", return_value=None), \
         patch("api.services.knowledge.librarian_core._upsert_thought", side_effect=lambda _u, row, body: dict(row, body=body)) as upsert:
        result = evolve_skill_from_plan(
            user_id=1,
            executor_ai_config_id=7,
            plan_id="plan-1",
            goal="整理浏览器登录流程",
            outcome="success",
            summary="已完成并验证登录状态",
            phases=[{"title": "打开登录页", "summary": "页面可访问"}, {"title": "验证状态", "summary": "检测到已登录"}],
        )

    assert result["evolved"] is True
    assert result["created"] is True
    row = upsert.call_args.args[1]
    assert row["scope"] == "ai"
    assert row["scope_target"] == "7"
    assert row["source"] == "auto:plan"
    assert row["version"] == "1"
    assert "## 执行步骤" in result["skill"]["body"]


def test_failed_plan_does_not_publish_a_skill():
    result = evolve_skill_from_plan(
        user_id=1,
        executor_ai_config_id=7,
        plan_id="plan-2",
        goal="危险操作",
        outcome="failure",
        summary="未完成",
        phases=[{"title": "失败", "summary": ""}],
    )
    assert result == {"evolved": False, "reason": "not_successful_or_missing_goal"}
