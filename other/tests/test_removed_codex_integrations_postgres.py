import os

import pytest
import sqlalchemy as sa


@pytest.mark.integration
def test_retired_codex_schema_is_absent_at_alembic_head() -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        ai_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("assistantaiconfig")
        }

    assert not tables.intersection(
        {
            "maintenanceapproval",
            "maintenanceevent",
            "maintenancetask",
            "externalcontrollerturn",
            "externalcontrollerevent",
            "externalcontrollerrun",
            "externalcontrollercredential",
        }
    )
    assert "execution_mode" not in ai_columns
