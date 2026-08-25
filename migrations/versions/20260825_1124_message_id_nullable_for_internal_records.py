"""message id nullable for internal records

Some records have no Telegram message behind them - an internal note taken
outside a topic, for example. NULL is the honest value, and NULLs do not
collide under the (chat_id, message_id) unique constraint the way a shared
placeholder of 0 did.

Uses batch mode because SQLite has no ALTER COLUMN. A plain `alter_column`
here runs silently and changes nothing on SQLite, which is worse than failing.

Revision ID: 5e5dc2c15711
Revises: 71b7b002220b
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '5e5dc2c15711'
down_revision = '71b7b002220b'
branch_labels = None
depends_on = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def upgrade() -> None:
    with op.batch_alter_table("messages", naming_convention=NAMING_CONVENTION) as batch:
        batch.alter_column(
            "telegram_message_id", existing_type=sa.BigInteger(), nullable=True
        )


def downgrade() -> None:
    # Rows written since the upgrade may hold NULL; give them a placeholder
    # rather than failing the downgrade outright.
    op.execute("UPDATE messages SET telegram_message_id = 0 WHERE telegram_message_id IS NULL")
    with op.batch_alter_table("messages", naming_convention=NAMING_CONVENTION) as batch:
        batch.alter_column(
            "telegram_message_id", existing_type=sa.BigInteger(), nullable=False
        )
