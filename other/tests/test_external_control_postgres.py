import os

import pytest
import sqlalchemy as sa


@pytest.mark.integration
def test_external_controller_schema_is_present_at_alembic_head() -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with engine.connect() as connection:
        tables = set(
            connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            ).scalars()
        )
        execution_mode = connection.execute(
            sa.text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'assistantaiconfig' AND column_name = 'execution_mode'"
            )
        ).scalar_one()

    assert execution_mode == "NO"
    assert {
        "externalcontrollercredential",
        "externalcontrollerrun",
        "externalcontrollerevent",
        "externalcontrollerturn",
    }.issubset(tables)
