"""add opaque bot contacts and conversation scope

Revision ID: b18c9d0e1f2a
Revises: a07b8c9d0e1f
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b18c9d0e1f2a"
down_revision: Union[str, Sequence[str], None] = "a07b8c9d0e1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("botconnection", sa.Column("connection_ref", sa.String(), nullable=False, server_default=""))
    op.add_column("botconnection", sa.Column("name", sa.String(), nullable=False, server_default=""))
    op.add_column("botconnection", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("botconnection", sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE botconnection SET connection_ref = 'conn_' || id::text WHERE connection_ref = ''")
    op.create_unique_constraint("uq_botconnection_connection_ref", "botconnection", ["connection_ref"])
    op.create_index("ix_botconnection_connection_ref", "botconnection", ["connection_ref"])
    op.create_index("ix_botconnection_enabled", "botconnection", ["enabled"])

    op.create_table(
        "botcontact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ai_config_id", sa.Integer(), nullable=False),
        sa.Column("contact_ref", sa.String(), nullable=False),
        sa.Column("external_id_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("target_encrypted", sa.String(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_proactive", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["botconnection.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["ai_config_id"], ["assistantaiconfig.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_ref"),
        sa.UniqueConstraint("connection_id", "external_id_hash", name="uq_bot_contact_connection_identity"),
    )
    for column in ("connection_id", "user_id", "ai_config_id", "contact_ref", "external_id_hash", "enabled"):
        op.create_index(f"ix_botcontact_{column}", "botcontact", [column])

    op.add_column("botsessionroute", sa.Column("connection_id", sa.Integer(), nullable=True))
    op.add_column("botsessionroute", sa.Column("contact_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_botroute_connection", "botsessionroute", "botconnection", ["connection_id"], ["id"])
    op.create_foreign_key("fk_botroute_contact", "botsessionroute", "botcontact", ["contact_id"], ["id"])
    op.create_index("ix_botsessionroute_connection_id", "botsessionroute", ["connection_id"])
    op.create_index("ix_botsessionroute_contact_id", "botsessionroute", ["contact_id"])

    op.add_column("botusercursor", sa.Column("contact_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_botcursor_contact", "botusercursor", "botcontact", ["contact_id"], ["id"])
    op.create_index("ix_botusercursor_contact_id", "botusercursor", ["contact_id"])

    op.add_column("chatsession", sa.Column("bot_connection_id", sa.Integer(), nullable=True))
    op.add_column("chatsession", sa.Column("bot_contact_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_chatsession_bot_connection", "chatsession", "botconnection", ["bot_connection_id"], ["id"])
    op.create_foreign_key("fk_chatsession_bot_contact", "chatsession", "botcontact", ["bot_contact_id"], ["id"])
    op.create_index("ix_chatsession_bot_connection_id", "chatsession", ["bot_connection_id"])
    op.create_index("ix_chatsession_bot_contact_id", "chatsession", ["bot_contact_id"])


def downgrade() -> None:
    for table, column in (("chatsession", "bot_contact_id"), ("chatsession", "bot_connection_id")):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
    op.drop_constraint("fk_chatsession_bot_contact", "chatsession", type_="foreignkey")
    op.drop_constraint("fk_chatsession_bot_connection", "chatsession", type_="foreignkey")
    op.drop_column("chatsession", "bot_contact_id")
    op.drop_column("chatsession", "bot_connection_id")
    op.drop_index("ix_botusercursor_contact_id", table_name="botusercursor")
    op.drop_constraint("fk_botcursor_contact", "botusercursor", type_="foreignkey")
    op.drop_column("botusercursor", "contact_id")
    op.drop_index("ix_botsessionroute_contact_id", table_name="botsessionroute")
    op.drop_index("ix_botsessionroute_connection_id", table_name="botsessionroute")
    op.drop_constraint("fk_botroute_contact", "botsessionroute", type_="foreignkey")
    op.drop_constraint("fk_botroute_connection", "botsessionroute", type_="foreignkey")
    op.drop_column("botsessionroute", "contact_id")
    op.drop_column("botsessionroute", "connection_id")
    op.drop_table("botcontact")
    op.drop_index("ix_botconnection_enabled", table_name="botconnection")
    op.drop_index("ix_botconnection_connection_ref", table_name="botconnection")
    op.drop_constraint("uq_botconnection_connection_ref", "botconnection", type_="unique")
    for column in ("is_default", "enabled", "name", "connection_ref"):
        op.drop_column("botconnection", column)
