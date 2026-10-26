from api.runtime.gateway_periodic import GatewayPeriodicJobs


def test_gateway_periodic_jobs_runs_maintenance_on_configured_cadence():
    calls = []
    jobs = GatewayPeriodicJobs(
        start_bot_connections=lambda: calls.append("bots"),
        process_task_scheduler=lambda: calls.append("scheduler"),
        reap_stale_runs=lambda: calls.append("heartbeat") or 0,
        cleanup_temp_images=lambda: calls.append("images") or 0,
        sync_remote_update_state=lambda: calls.append("sync"),
        auto_check_repo_update=lambda: calls.append("auto"),
        maintenance_every=2,
    )

    jobs.run_once()
    assert calls == ["bots", "scheduler"]

    jobs.run_once()
    assert calls == [
        "bots", "scheduler", "bots", "scheduler",
        "heartbeat", "images", "sync", "auto",
    ]


def test_gateway_periodic_jobs_isolates_failed_jobs():
    calls = []

    def fail():
        calls.append("failed")
        raise RuntimeError("expected test failure")

    jobs = GatewayPeriodicJobs(
        start_bot_connections=fail,
        process_task_scheduler=lambda: calls.append("scheduler"),
        reap_stale_runs=lambda: calls.append("heartbeat") or 1,
        cleanup_temp_images=lambda: calls.append("images") or 0,
        sync_remote_update_state=lambda: calls.append("sync"),
        auto_check_repo_update=lambda: calls.append("auto"),
        maintenance_every=1,
    )

    jobs.run_once()

    assert calls == ["failed", "scheduler", "heartbeat", "images", "sync", "auto"]
