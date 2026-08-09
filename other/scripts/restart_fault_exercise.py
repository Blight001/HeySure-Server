"""Repeat service restarts and assert readiness plus durable task invariants."""

from __future__ import annotations

import argparse

if __package__:
    from .rolling_release import SERVICES, compose, wait_ready
else:
    from rolling_release import SERVICES, compose, wait_ready


STALE_SQL = """
SELECT
  (SELECT count(*) FROM chatrun
    WHERE status = 'running' AND lease_expires_at IS NOT NULL
      AND lease_expires_at < extract(epoch FROM now()))
  +
  (SELECT count(*) FROM agentdispatchtask
    WHERE status IN ('pending', 'queued') AND deadline_at IS NOT NULL
      AND deadline_at < extract(epoch FROM now()));
""".strip()


def smoke(
    internal_token: str,
    timeout: float,
    account: str,
    password: str,
) -> None:
    try:
        compose(
            "exec", "-T", "api-gateway", "python",
            "other/scripts/smoke_four_runtime.py",
            "--gateway", "http://api-gateway:3000",
            "--connector", "http://connector-runtime:3002",
            "--internal-token", internal_token,
            "--account", account,
            "--password", password,
            "--timeout", str(timeout),
        )
    except RuntimeError:
        # ``rolling_release.command`` includes argv in its exception. Replace it
        # here so the internal bearer token is never echoed by this harness.
        raise RuntimeError("four-runtime smoke failed") from None


def stale_task_count() -> int:
    value = compose(
        "exec", "-T", "db", "psql", "-U", "heysure", "-d", "heysure",
        "-Atc", STALE_SQL, capture=True,
    )
    return int(value.strip() or "0")


def exercise(
    iterations: int,
    internal_token: str,
    timeout: float,
    account: str,
    password: str,
) -> None:
    for iteration in range(1, iterations + 1):
        for service, port in SERVICES:
            compose("restart", service)
            wait_ready(service, port, timeout)
        smoke(internal_token, timeout, account, password)
        stale = stale_task_count()
        if stale:
            raise RuntimeError(
                f"iteration {iteration}: {stale} expired running/pending tasks remain"
            )
        print(f"restart exercise {iteration}/{iterations}: passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--internal-token", required=True)
    parser.add_argument("--account", default="runtime-smoke")
    parser.add_argument("--password", default="runtime-smoke")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    try:
        exercise(
            max(1, args.iterations),
            args.internal_token,
            max(10.0, args.timeout),
            args.account,
            args.password,
        )
    except Exception as exc:
        print(f"restart fault exercise failed: {exc}")
        return 1
    print("restart fault exercise completed without permanent task states")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
