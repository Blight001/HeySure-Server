import os

import pytest
import sqlalchemy as sa


@pytest.mark.integration
def test_maintenance_schema_is_present_at_alembic_head() -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"maintenancetask", "maintenanceevent", "maintenanceapproval"}.issubset(tables)

    task_columns = {column["name"] for column in inspector.get_columns("maintenancetask")}
    assert {
        "task_id", "run_id", "status", "phase", "owner", "lease_expires_at",
        "deadline_at", "last_sequence", "last_device_sequence", "branch_name", "base_sha",
    }.issubset(task_columns)

    event_unique = {
        tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints("maintenanceevent")
    }
    assert ("run_id", "event_id") in event_unique
    assert ("run_id", "sequence") in event_unique

    approval_columns = {column["name"] for column in inspector.get_columns("maintenanceapproval")}
    assert {"approval_id", "task_id", "status", "decision", "expires_at"}.issubset(
        approval_columns
    )
