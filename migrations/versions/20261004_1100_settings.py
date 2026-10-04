"""a value somebody can change without a deploy

Jason, 3 October: "need the ability to change monitoring of wallet."

That rules out an environment variable. Changing one means a restart and
somebody with shell access, and the person who needs to change a wallet
address is on the finance desk.

A key-value table rather than a column per setting: there is exactly one
setting today, the shape of the second is unknown, and a table that can hold
either is cheaper than guessing. Nothing secret goes in it - the bot token
and the database password stay in the environment, where they are not one
command away from being printed into a Telegram group.

Revision ID: 2b9f41ac6d83
Revises: 0a7c93d15e26
Create Date: 2026-10-04 11:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '2b9f41ac6d83'
down_revision = '0a7c93d15e26'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=True),
        sa.Column("updated_by_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )


def downgrade() -> None:
    op.drop_table("settings")
