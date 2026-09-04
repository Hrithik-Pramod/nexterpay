"""nominated leads for counterparty groups

Telegram will not tell a bot who is in a group. There is no API call for it,
and that is deliberate on Telegram's part rather than an omission. So instead
of discovering the people in a client or supplier group, NexterPay name them -
reply to one of their messages with a command, exactly as staff are registered,
and the bot learns who they are.

One mechanism, two features. Each counterparty group gets a named lead, and
"send this to a person rather than to the room" becomes possible, which was
asked for separately a week earlier.

Per chat rather than per client, because a client with a Support group and a
Finance group usually has a different contact in each.

Revision ID: a3d1f70c95b8
Revises: 8f4a2be05c19
Create Date: 2026-09-04 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a3d1f70c95b8'
down_revision = '8f4a2be05c19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.id"], name="fk_group_leads_chat_id_chats"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_group_leads"),
        sa.UniqueConstraint("chat_id", "telegram_user_id", name="uq_group_lead"),
    )
    op.create_index("ix_group_leads_chat", "group_leads", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_group_leads_chat", table_name="group_leads")
    op.drop_table("group_leads")
