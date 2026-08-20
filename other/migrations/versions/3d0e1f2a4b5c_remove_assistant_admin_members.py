"""remove the retired assistant-admin members and their database data

Revision ID: 3d0e1f2a4b5c
Revises: 2c9d0e1f3a4b
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "3d0e1f2a4b5c"
down_revision: Union[str, Sequence[str], None] = "2c9d0e1f3a4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Child records are removed before their parents.  The helper deliberately checks
# the live schema because older adopted databases may not contain every optional
# feature table yet.
_DEPENDENT_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("externalcontrollerevent", ("ai_config_id",)),
    ("externalcontrollerturn", ("ai_config_id",)),
    ("externalcontrollerrun", ("ai_config_id",)),
    ("externalcontrollercredential", ("ai_config_id",)),
    ("aimessage", ("from_ai_config_id", "to_ai_config_id")),
    ("maintenancetask", ("maintainer_ai_config_id", "reporter_ai_config_id")),
    ("workflowconfirmation", ("ai_config_id",)),
    ("workflowrecording", ("ai_config_id",)),
    ("botsessionroute", ("ai_config_id",)),
    ("botusercursor", ("ai_config_id",)),
    ("botcontact", ("ai_config_id",)),
    ("botconnection", ("ai_config_id",)),
    ("workshopaibinding", ("ai_config_id",)),
    ("deviceaibinding", ("ai_config_id",)),
    ("agentdispatchtask", ("ai_config_id",)),
    ("aitaskjob", ("ai_config_id", "created_by_ai_config_id")),
    ("taskplan", ("ai_config_id",)),
    ("chatrun", ("ai_config_id",)),
    ("chatmessageattachment", ("ai_config_id",)),
    ("chatmessage", ("ai_config_id",)),
    ("chatsession", ("ai_config_id",)),
    ("tokenusagesnapshot", ("ai_config_id",)),
    ("airuntimestatus", ("ai_config_id",)),
    ("memory", ("ai_config_id",)),
    ("knowledgeentry", ("source_ai_config_id", "librarian_ai_config_id")),
    ("mcptoolstat", ("ai_config_id",)),
    ("mcpfailureevent", ("ai_config_id",)),
    ("devicetypemcppermission", ("ai_config_id",)),
    ("devicedynamictoolversion", ("ai_config_id",)),
    ("devicepresence", ("ai_config_id",)),
    ("worldactormeta", ("ai_config_id",)),
    ("usernotification", ("ai_config_id",)),
)


def _delete_related_rows(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if {"botconnection", "botsessionroute"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM botsessionroute WHERE connection_id IN ("
            "SELECT id FROM botconnection WHERE ai_config_id IN "
            "(SELECT id FROM _retired_assistant_admin_ids))"
        ))
    if {"botconnection", "botcontact", "botusercursor"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM botusercursor WHERE contact_id IN (SELECT id FROM botcontact WHERE connection_id IN ("
            "SELECT id FROM botconnection WHERE ai_config_id IN "
            "(SELECT id FROM _retired_assistant_admin_ids)))"
        ))
    if {"botconnection", "botcontact", "botsessionroute"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM botsessionroute WHERE contact_id IN (SELECT id FROM botcontact WHERE connection_id IN ("
            "SELECT id FROM botconnection WHERE ai_config_id IN "
            "(SELECT id FROM _retired_assistant_admin_ids)))"
        ))
    if {"botconnection", "botcontact"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM botcontact WHERE connection_id IN (SELECT id FROM botconnection WHERE ai_config_id IN "
            "(SELECT id FROM _retired_assistant_admin_ids))"
        ))
    if {"externalcontrollercredential", "externalcontrollerevent"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM externalcontrollerevent WHERE credential_id IN (SELECT id FROM externalcontrollercredential "
            "WHERE ai_config_id IN (SELECT id FROM _retired_assistant_admin_ids))"
        ))
    if {"externalcontrollercredential", "externalcontrollerturn"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM externalcontrollerturn WHERE credential_id IN (SELECT id FROM externalcontrollercredential "
            "WHERE ai_config_id IN (SELECT id FROM _retired_assistant_admin_ids))"
        ))
    if {"externalcontrollercredential", "externalcontrollerrun"} <= tables:
        bind.execute(sa.text(
            "DELETE FROM externalcontrollerrun WHERE credential_id IN (SELECT id FROM externalcontrollercredential "
            "WHERE ai_config_id IN (SELECT id FROM _retired_assistant_admin_ids))"
        ))
    for table, candidate_columns in _DEPENDENT_COLUMNS:
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        predicates = [
            f'"{column}" IN (SELECT id FROM _retired_assistant_admin_ids)'
            for column in candidate_columns
            if column in columns
        ]
        if predicates:
            bind.execute(sa.text(f'DELETE FROM "{table}" WHERE ' + " OR ".join(predicates)))


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "CREATE TEMPORARY TABLE _retired_assistant_admin_ids ON COMMIT DROP AS "
            "SELECT id FROM assistantaiconfig WHERE ai_role = 'assistant_admin'"
        )
    )
    _delete_related_rows(bind)
    bind.execute(
        sa.text(
            "UPDATE assistantaiconfig SET parent_ai_config_id = NULL "
            "WHERE parent_ai_config_id IN (SELECT id FROM _retired_assistant_admin_ids)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE assistantaiconfig SET root_manager_ai_config_id = NULL "
            "WHERE root_manager_ai_config_id IN (SELECT id FROM _retired_assistant_admin_ids)"
        )
    )
    bind.execute(
        sa.text("DELETE FROM assistantaiconfig WHERE id IN (SELECT id FROM _retired_assistant_admin_ids)")
    )


def downgrade() -> None:
    # Removed members may contain credentials, conversations and task history;
    # reconstructing partial rows would be unsafe and misleading.
    pass
