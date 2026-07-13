"""Regression coverage for the plan-completion knowledge-review briefing."""

import unittest

from ai_runtime.inference.core import _should_replay_system_notice_as_user
from api.services.knowledge.knowledge_review_trigger import (
    KNOWLEDGE_REVIEW_TAG,
    _render_briefing,
)


class KnowledgeReviewBriefingTests(unittest.TestCase):
    def test_review_system_notice_is_replayed_to_the_model(self) -> None:
        self.assertTrue(_should_replay_system_notice_as_user(KNOWLEDGE_REVIEW_TAG))

    def test_unrelated_system_bubble_is_not_replayed(self) -> None:
        self.assertFalse(_should_replay_system_notice_as_user("system_notice_ai_error"))

    def test_briefing_contains_the_actionable_review_request(self) -> None:
        content = _render_briefing(
            executor_name="执行者",
            goal="完成测试任务",
            outcome="success",
            summary="已完成",
            phases=[],
            log_path="",
        )

        self.assertIn("【计划完成 · 待你审核是否沉淀】", content)
        self.assertIn("knowledge.manage(action=record_experience)", content)


if __name__ == "__main__":
    unittest.main()
