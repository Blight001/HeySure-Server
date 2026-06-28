"""add composite index for chat history paging

Chat history is fetched with
``WHERE user_id=? AND session_id=? AND ai_kind=? [AND ai_config_id=?]
  ORDER BY id DESC LIMIT N`` (see ``gateway/routers/chat_history_routes.py``).
The existing single-column indexes force Postgres to bitmap-AND several of them
and sort, which gets slow as a user's message table grows. A composite index on
``(user_id, session_id, ai_kind, id)`` lets the planner satisfy the equality
filters and the ``id`` ordering from one index (scanned backward for DESC),
turning the latest-page / incremental-tail / cursor-paging reads into a bounded
index range scan.

Idempotent: skipped if the table or the index already exists.

Revision ID: f1a2b3c4d5e6
Revises: d5e6f7a8b9c0
Create Date: 2026-06-28

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_chatmessage_history"
TABLE_NAME = "chatmessage"
COLUMNS = ["user_id", "session_id", "ai_kind", "id"]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if TABLE_NAME not in set(inspector.get_table_names()):
        return
    existing = {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME in existing:
        return
    op.create_index(INDEX_NAME, TABLE_NAME, COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if TABLE_NAME not in set(inspector.get_table_names()):
        return
    existing = {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME not in existing:
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
