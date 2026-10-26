"""独立、可测试的 Gateway 周期任务协调器。

Gateway 仍然拥有这些任务的生命周期，但每个任务通过独立回调和错误边界
执行，避免某个后台任务阻塞或抛错后影响其他任务。
"""

from __future__ import annotations

import logging
from typing import Callable


logger = logging.getLogger(__name__)
Job = Callable[[], object]


class GatewayPeriodicJobs:
    """Run Gateway maintenance jobs with explicit cadence and isolation."""

    def __init__(
        self,
        *,
        start_bot_connections: Job,
        process_task_scheduler: Job,
        reap_stale_runs: Job,
        cleanup_temp_images: Job,
        sync_remote_update_state: Job,
        auto_check_repo_update: Job,
        maintenance_every: int = 10,
    ) -> None:
        self._start_bot_connections = start_bot_connections
        self._process_task_scheduler = process_task_scheduler
        self._maintenance_jobs = (
            ("watchdog reap", reap_stale_runs),
            ("temporary image cleanup", cleanup_temp_images),
            ("repo-update state sync", sync_remote_update_state),
            ("repo-update auto check", auto_check_repo_update),
        )
        self._maintenance_every = max(1, int(maintenance_every))
        self._ticks = 0

    def _run(self, label: str, job: Job) -> object:
        try:
            return job()
        except Exception:
            logger.exception("%s failed", label)
            return None

    def run_once(self) -> None:
        """Run one short scheduler tick without allowing failures to escape."""
        self._run("start gateway bot long connections", self._start_bot_connections)
        self._run("process_task_scheduler", self._process_task_scheduler)
        self._ticks += 1
        if self._ticks % self._maintenance_every:
            return
        for label, job in self._maintenance_jobs:
            result = self._run(label, job)
            if label == "watchdog reap" and result:
                logger.warning("watchdog reaped stale runs: %s", result)
            elif label == "temporary image cleanup" and result:
                logger.info("removed expired temporary images: %s", result)
