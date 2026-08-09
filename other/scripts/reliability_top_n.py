"""Emit a sanitized PostgreSQL reliability Top-N snapshot as JSON."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from sqlalchemy import text


SERVER_ROOT = Path(__file__).resolve().parents[2]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from api.database import engine


QUERY_SPECS = {
    "slow_model_turns": """
        SELECT id, user_id, ai_config_id, model,
               ROUND(CAST(latency AS numeric), 3) AS latency_seconds,
               created_at
        FROM chatmessage
        WHERE latency IS NOT NULL
        ORDER BY latency DESC NULLS LAST, id DESC
        LIMIT :limit
    """,
    "long_transactions": """
        SELECT pid, usename, application_name, state,
               wait_event_type, wait_event,
               CAST(EXTRACT(EPOCH FROM (now() - xact_start)) AS bigint) AS age_seconds
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND xact_start IS NOT NULL
        ORDER BY age_seconds DESC, pid
        LIMIT :limit
    """,
    "lock_waiters": """
        SELECT pid, usename, application_name, state, wait_event,
               CAST(EXTRACT(EPOCH FROM (now() - query_start)) AS bigint) AS wait_seconds
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND wait_event_type = 'Lock'
        ORDER BY wait_seconds DESC, pid
        LIMIT :limit
    """,
    "chat_run_queue": """
        SELECT run_id, user_id, ai_config_id, status,
               worker_instance_id, attempt,
               CAST(GREATEST(0, EXTRACT(EPOCH FROM now()) -
                    COALESCE(started_at, created_at)) AS bigint) AS age_seconds
        FROM chatrun
        WHERE status IN ('queued', 'running')
        ORDER BY age_seconds DESC, id
        LIMIT :limit
    """,
    "dispatch_queue": """
        SELECT task_id, user_id, ai_config_id, device_id, tool, status,
               owner_instance_id, attempt,
               CAST(GREATEST(0, EXTRACT(EPOCH FROM now()) - created_at) AS bigint)
                   AS age_seconds
        FROM agentdispatchtask
        WHERE status IN ('queued', 'pending')
        ORDER BY age_seconds DESC, id
        LIMIT :limit
    """,
}


def collect_top_n(connection, limit: int) -> dict:
    snapshot = {
        "generated_at": time.time(),
        "limit": limit,
        "categories": {},
    }
    for name, statement in QUERY_SPECS.items():
        rows = connection.execute(
            text(statement),
            {"limit": limit},
        ).mappings().all()
        snapshot["categories"][name] = [dict(row) for row in rows]
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    limit = min(100, max(1, args.limit))
    with engine.connect() as connection:
        snapshot = collect_top_n(connection, limit)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
