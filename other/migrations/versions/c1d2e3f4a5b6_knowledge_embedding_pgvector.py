"""switch knowledge embedding storage to pgvector

Replaces the JSON ``embedding`` column on ``knowledgeembedding`` with a native
pgvector ``vector(N)`` column and adds an HNSW index for cosine similarity, so
semantic recall runs as an indexed ``ORDER BY embedding <=> :q LIMIT k`` query
instead of an in-process full scan.

Embeddings are a rebuildable cache (the markdown topics under
``KnowledgeBase/topics`` are the source of truth), so the migration clears the
table and lets the service layer re-embed lazily. The column dimension follows
``settings.embedding_dimensions``.

Revision ID: c1d2e3f4a5b6
Revises: b0a1c2d3e4f5
Create Date: 2026-06-20

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b0a1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

try:  # Keep the column width in sync with the runtime embedding config.
    from api.core.settings import settings

    _DIMENSIONS = int(getattr(settings, "embedding_dimensions", 1536) or 1536)
except Exception:  # pragma: no cover - settings unavailable in some tooling
    _DIMENSIONS = 1536

_INDEX = "ix_knowledgeembedding_embedding_hnsw"


def _bind():
    return op.get_bind()


def _is_postgres() -> bool:
    return _bind().dialect.name == "postgresql"


def _has_table(table_name: str) -> bool:
    return table_name in inspect(_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("knowledgeembedding"):
        return
    # pgvector is a PostgreSQL extension; on other backends the JSON column and
    # the in-process scan fallback remain in use.
    if not _is_postgres():
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Stale JSON vectors are not castable across an embedding-model change and
    # are cheap to rebuild, so drop them rather than attempt an in-place cast.
    op.execute("DELETE FROM knowledgeembedding")
    op.execute("ALTER TABLE knowledgeembedding DROP COLUMN IF EXISTS embedding")
    op.execute(f"ALTER TABLE knowledgeembedding ADD COLUMN embedding vector({_DIMENSIONS})")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        "ON knowledgeembedding USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    if not _has_table("knowledgeembedding") or not _is_postgres():
        return
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("DELETE FROM knowledgeembedding")
    op.execute("ALTER TABLE knowledgeembedding DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE knowledgeembedding ADD COLUMN embedding json NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE knowledgeembedding ALTER COLUMN embedding DROP DEFAULT")
