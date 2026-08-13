"""allow multiple bot accounts per AI and channel

Revision ID: d30e1f2a3b4c
Revises: b18c9d0e1f2a
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d30e1f2a3b4c"
down_revision: Union[str, Sequence[str], None] = "b18c9d0e1f2a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_bot_connection_channel_config", "botconnection", type_="unique")
    op.drop_constraint("uq_bot_connection_provider_account", "botconnection", type_="unique")
    op.create_index(
        "uq_botconnection_provider_account_nonempty",
        "botconnection",
        ["channel", "provider_account_id"],
        unique=True,
        postgresql_where=sa.text("provider_account_id <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_botconnection_provider_account_nonempty", table_name="botconnection")
    # Downgrade deliberately keeps only one instance per channel.
    op.execute("""
        DELETE FROM botconnection newer
        USING botconnection older
        WHERE newer.channel = older.channel
          AND newer.ai_config_id = older.ai_config_id
          AND newer.id > older.id
    """)
    op.create_unique_constraint(
        "uq_bot_connection_channel_config", "botconnection", ["channel", "ai_config_id"]
    )
    op.create_unique_constraint(
        "uq_bot_connection_provider_account", "botconnection", ["channel", "provider_account_id"]
    )
