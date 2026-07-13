"""Todo runs survive model-side natural stops until the plan is finished."""

import unittest

from ai_runtime.inference.core import _can_start_inference_step, _has_active_todo_plan


class TodoRunPersistenceTests(unittest.TestCase):
    def test_unfinished_todo_keeps_the_run_alive(self) -> None:
        self.assertTrue(_has_active_todo_plan(object()))

    def test_missing_or_finished_todo_does_not_keep_the_run_alive(self) -> None:
        self.assertFalse(_has_active_todo_plan(None))

    def test_active_todo_bypasses_the_ordinary_step_limit(self) -> None:
        self.assertTrue(_can_start_inference_step(48, 48, object()))
        self.assertFalse(_can_start_inference_step(48, 48, None))


if __name__ == "__main__":
    unittest.main()
