"""broadcasts and supplier groups

Adds the flag that separates supplier groups from client groups, and the two
tables behind broadcasting.

The flag is on the chat rather than being a third ChatKind. Suppliers behave
exactly as clients do - NexterPay confirmed it is the same process with
different labels - so a new kind would mean revisiting every permission and
routing decision that turns on ChatKind.CLIENT. Only broadcasting needs to
tell them apart.

Revision ID: c41d8e0b6a52
Revises: 9a2c4f1e77b3
Create Date: 2026-08-31 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c41d8e0b6a52'
down_revision = '9a2c4f1e77b3'
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
    # server_default is required, not cosmetic: the column is NOT NULL and
    # existing rows need a value. Every group registered so far is a client
    # group, which is what false means.
    with op.batch_alter_table("chats", naming_convention=NAMING_CONVENTION) as batch:
        batch.add_column(
            sa.Column(
                "is_supplier",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sent_by_staff_id", sa.Integer(), nullable=True),
        sa.Column("sent_by_name", sa.String(length=200), nullable=False),
        sa.Column("audience", sa.String(length=60), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["sent_by_staff_id"], ["staff.id"], name="fk_broadcasts_sent_by_staff_id_staff"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broadcasts"),
    )

    op.create_table(
        "broadcast_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broadcast_id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_title", sa.String(length=300), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            name="fk_broadcast_deliveries_broadcast_id_broadcasts",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broadcast_deliveries"),
    )
    op.create_index(
        "ix_broadcast_deliveries_broadcast_id",
        "broadcast_deliveries",
        ["broadcast_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_deliveries_broadcast_id", table_name="broadcast_deliveries")
    op.drop_table("broadcast_deliveries")
    op.drop_table("broadcasts")
    with op.batch_alter_table("chats", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_column("is_supplier")
