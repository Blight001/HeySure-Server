import os

import pytest
import sqlalchemy as sa


@pytest.mark.integration
def test_external_member_mcp_schema_at_alembic_head() -> None:
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        ai_columns = {item["name"] for item in inspector.get_columns("assistantaiconfig")}
        credential_columns = {
            item["name"] for item in inspector.get_columns("externalmcpcredential")
        }
        audit_columns = {
            item["name"] for item in inspector.get_columns("externalmcpcallaudit")
        }
        credential_indexes = {
            item["name"]: item for item in inspector.get_indexes("externalmcpcredential")
        }

    assert {"external_mcp_enabled", "external_mcp_public_id"} <= ai_columns
    assert {"token_hash", "token_prefix", "expires_at", "last_used_at", "revoked_at"} <= credential_columns
    assert credential_indexes["ix_externalmcpcredential_token_hash"]["unique"] is True
    assert {"protocol_method", "tool_name", "success", "error_code", "duration_ms"} <= audit_columns
    assert not {"arguments", "result", "body", "token", "token_hash"}.intersection(audit_columns)
