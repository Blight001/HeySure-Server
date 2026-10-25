"""PostgreSQL persistence contract for asynchronous MCP programs."""

import time
import uuid

import pytest
from sqlmodel import Session

from api.database import engine
from api.models import McpProgramRun, User
from api.services.workflows.secrets import decrypt_json, encrypt_json


pytestmark = pytest.mark.integration


def test_postgres_persists_owner_scoped_mcp_program_run():
    suffix = uuid.uuid4().hex
    now = time.time()
    with Session(engine) as session:
        user = User(name="mcp-program-test", account=f"mcp-program-{suffix}", hashed_password="x")
        session.add(user)
        session.commit()
        session.refresh(user)
        row = McpProgramRun(
            id=f"mcpprogram_test_{suffix}",
            user_id=user.id,
            status="queued",
            program_json=encrypt_json({"steps": []}),
            input_json=encrypt_json({"query": "x"}),
            allowed_tools_json=encrypt_json(["knowledge.search"]),
            session_context_json=encrypt_json({"session_id": "test"}),
            deadline_at=now + 60,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        user_id, run_id = user.id, row.id

    try:
        with Session(engine) as session:
            persisted = session.get(McpProgramRun, run_id)
            assert persisted is not None
            assert persisted.user_id == user_id
            assert persisted.status == "queued"
            assert decrypt_json(persisted.input_json) == {"query": "x"}
            assert decrypt_json(persisted.allowed_tools_json) == ["knowledge.search"]
    finally:
        with Session(engine) as session:
            persisted = session.get(McpProgramRun, run_id)
            if persisted:
                session.delete(persisted)
            user = session.get(User, user_id)
            if user:
                session.delete(user)
            session.commit()
