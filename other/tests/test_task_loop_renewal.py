import json
from datetime import datetime
from zoneinfo import ZoneInfo

from api.chat_runtime.chat_runtime_helpers import _renew_loop_scheduled_job
from api.models import AITaskJob
from api.services.tasks import task_schedule


class _Session:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


def _loop_job(**schedule_overrides):
    schedule = {
        "enabled": True,
        "loop_enabled": True,
        "loop_mode": "interval",
        "duration_minutes": 5,
        "runs_done": 0,
        "max_runs": 0,
        **schedule_overrides,
    }
    return AITaskJob(
        job_id="job_same",
        user_id=1,
        ai_config_id=2,
        title="循环任务",
        instruction="执行",
        trigger_type="schedule",
        status="running",
        task_payload=json.dumps({"schedule": schedule}),
        started_at=100.0,
        finished_at=200.0,
        last_supervised_at=150.0,
        supervision_count=3,
        completion_notified_at=199.0,
    )


def test_loop_job_is_renewed_in_place_and_stays_editable():
    session = _Session()
    job = _loop_job()

    renewed = _renew_loop_scheduled_job(session, job, 1_000.0)

    assert renewed is job
    assert renewed.job_id == "job_same"
    assert renewed.status == "queued"
    assert renewed.started_at is None
    assert renewed.finished_at is None
    assert renewed.last_supervised_at is None
    assert renewed.supervision_count == 0
    assert renewed.completion_notified_at is None
    assert renewed.updated_at == 1_000.0
    assert session.added == [job]
    schedule = json.loads(renewed.task_payload)["schedule"]
    assert schedule["runs_done"] == 1
    assert schedule["schedule_at"] == 1_300.0


def test_loop_job_renews_even_after_supervision_overwrote_trigger_type():
    # 调度器 supervision/preempt 派发会覆写 job.trigger_type；循环与否由
    # payload["schedule"] 决定，覆写后循环任务仍必须正常续期（历史 bug：
    # 被监督续跑过的循环任务在本轮结束时被直接标记 completed，循环断掉，
    # schedule_at 停留在旧值）。
    for overwritten in ("supervision", "preempt", "resume"):
        session = _Session()
        job = _loop_job()
        job.trigger_type = overwritten

        renewed = _renew_loop_scheduled_job(session, job, 1_000.0)

        assert renewed is job, overwritten
        assert renewed.status == "queued"
        assert renewed.trigger_type == "schedule"
        schedule = json.loads(renewed.task_payload)["schedule"]
        assert schedule["runs_done"] == 1
        assert schedule["schedule_at"] == 1_300.0


def test_non_loop_job_is_not_renewed_regardless_of_trigger_type():
    session = _Session()
    job = _loop_job(loop_enabled=False)
    job.trigger_type = "manual"

    assert _renew_loop_scheduled_job(session, job, 1_000.0) is None
    assert job.status == "running"
    assert session.added == []


def test_loop_job_only_completes_after_run_limit():
    session = _Session()
    job = _loop_job(max_runs=1)

    assert _renew_loop_scheduled_job(session, job, 1_000.0) is None
    assert job.status == "running"
    assert session.added == []


def test_daily_schedule_uses_configured_wall_clock_timezone(monkeypatch):
    tz = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(task_schedule, "schedule_timezone", lambda: tz)
    now = datetime(2026, 7, 12, 9, 30, tzinfo=tz).timestamp()

    next_at = task_schedule.next_loop_occurrence(
        {"loop_mode": "daily", "daily_time": "10:00", "duration_minutes": 5},
        now,
    )

    assert datetime.fromtimestamp(next_at, tz).strftime("%Y-%m-%d %H:%M") == "2026-07-12 10:00"
