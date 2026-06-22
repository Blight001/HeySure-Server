import unittest
from unittest.mock import patch

from tools.tasks import _task_create


def _resolved_mode(arguments):
    with patch("tools.tasks._task_create_impl") as create_impl:
        create_impl.return_value = {"created": True}
        result = _task_create(1, arguments, 29)

    assert result == {"created": True}
    return create_impl.call_args.kwargs["mode"]


class TaskCreateModeTests(unittest.TestCase):
    def test_defaults_to_immediate_without_mode(self):
        self.assertEqual(
            _resolved_mode({"title": "整理购物车", "instruction": "记录前三件商品"}),
            "immediate",
        )

    def test_infers_scheduled_mode_from_schedule_at(self):
        self.assertEqual(_resolved_mode({
            "title": "稍后整理购物车",
            "instruction": "记录前三件商品",
            "schedule_at": "2026-06-14T18:00:00+08:00",
        }), "scheduled")

    def test_infers_recurring_mode_from_loop_fields(self):
        self.assertEqual(_resolved_mode({
            "title": "定期整理购物车",
            "instruction": "记录前三件商品",
            "schedule_loop_mode": "daily",
            "schedule_daily_time": "18:00",
        }), "recurring")

    def test_keeps_explicit_mode_aliases(self):
        self.assertEqual(_resolved_mode({
            "mode": "now",
            "title": "整理购物车",
            "instruction": "记录前三件商品",
        }), "immediate")


if __name__ == "__main__":
    unittest.main()
