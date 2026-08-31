"""connected tickets

The link table from section 3 of the filing structure document: any two
tickets can be tied together where the same underlying problem has produced
one from a client and another with a supplier.

The pair is stored ordered, lower id first, with a unique constraint over it.
That is what makes a link symmetric rather than directional - "A linked to B"
and "B linked to A" are the same row, so linking twice in opposite directions
cannot produce a duplicate that then shows the other ticket twice in a list.
The check constraint makes a self-link impossible for the same reason.

Nothing about a link is client-facing, so there is no data to backfill and no
message anyone will notice changing.

Revision ID: 4c9b17ad3e62
Revises: b58e2d740fa1
Create Date: 2026-08-31 19:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '4c9b17ad3e62'
down_revision = 'b58e2d740fa1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_item_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lower_work_item_id", sa.Integer(), nullable=False),
        sa.Column("higher_work_item_id", sa.Integer(), nullable=False),
        sa.Column("created_by_staff_id", sa.Integer(), nullable=True),
        sa.Column("created_by_name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["lower_work_item_id"],
            ["work_items.id"],
            name="fk_work_item_links_lower_work_item_id_work_items",
        ),
        sa.ForeignKeyConstraint(
            ["higher_work_item_id"],
            ["work_items.id"],
            name="fk_work_item_links_higher_work_item_id_work_items",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_staff_id"],
            ["staff.id"],
            name="fk_work_item_links_created_by_staff_id_staff",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_item_links"),
        sa.UniqueConstraint(
            "lower_work_item_id", "higher_work_item_id", name="uq_work_item_link"
        ),
        sa.CheckConstraint(
            "lower_work_item_id < higher_work_item_id", name="ck_work_item_link_order"
        ),
    )
    op.create_index(
        "ix_work_item_links_lower", "work_item_links", ["lower_work_item_id"]
    )
    op.create_index(
        "ix_work_item_links_higher", "work_item_links", ["higher_work_item_id"]
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'tickets_linked'")
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'tickets_unlinked'")


def downgrade() -> None:
    op.drop_index("ix_work_item_links_higher", table_name="work_item_links")
    op.drop_index("ix_work_item_links_lower", table_name="work_item_links")
    op.drop_table("work_item_links")
    # The two enum values stay. Postgres cannot remove one, and any events
    # already recorded with them would stop resolving if it could.
