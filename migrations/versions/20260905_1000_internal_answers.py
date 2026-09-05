"""a request remembers which one it was asked on behalf of

Ask another department opened a linked request on the other desk and stopped
there. The answer never came back - whoever asked had to go and read the other
topic to find it. That was documented as working, which is worse than it not
existing, because NexterPay chose it over transferring on that basis.

A link would nearly carry this and nearly is the problem. Links are unordered
and a request may hold several, so "which one do I answer back to?" has no
answer from the link table - it would have to be inferred from the ids, which
is right until somebody links a third ticket.

Revision ID: c7e2a9b41d63
Revises: a3d1f70c95b8
Create Date: 2026-09-05 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c7e2a9b41d63'
down_revision = 'a3d1f70c95b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table because SQLite cannot ALTER a column or add a named
    # foreign key in place; it rebuilds the table. Postgres ignores the
    # ceremony and does the straightforward thing.
    with op.batch_alter_table("work_items") as batch:
        batch.add_column(sa.Column("asked_from_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_work_items_asked_from_id", "work_items", ["asked_from_id"], ["id"]
        )

    # Nothing to backfill. Requests raised before this migration were opened
    # by Ask another department without any way to answer back, so there is no
    # origin recorded anywhere to recover - not in a column, not in an event.
    # They keep their link, which is what they had.

    if op.get_bind().dialect.name == "postgresql":
        # ADD VALUE cannot run inside a transaction block on older servers,
        # and IF NOT EXISTS makes the whole thing safe to re-run.
        op.execute(
            "ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'internal_answer_sent'"
        )


def downgrade() -> None:
    with op.batch_alter_table("work_items") as batch:
        batch.drop_constraint("fk_work_items_asked_from_id", type_="foreignkey")
        batch.drop_column("asked_from_id")

    # The enum value is deliberately left in place. Postgres cannot drop a
    # value from an enum, and any events already written with it would become
    # unreadable rows if it could. A spare value nobody emits is harmless.
