"""outbound requests

Records which direction a request was raised in. NexterPay's team can now
open one into a client or supplier group rather than only receiving them, and
"what we raised with this supplier" has to stay separable from "what this
client raised with us".

Revision ID: b58e2d740fa1
Revises: e7f3a91c04d8
Create Date: 2026-08-31 17:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b58e2d740fa1'
down_revision = 'e7f3a91c04d8'
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
    # server_default because the column is NOT NULL and existing rows need a
    # value. Everything raised so far came from a client, which is false.
    with op.batch_alter_table("work_items", naming_convention=NAMING_CONVENTION) as batch:
        batch.add_column(
            sa.Column(
                "raised_by_us", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("work_items", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_column("raised_by_us")
