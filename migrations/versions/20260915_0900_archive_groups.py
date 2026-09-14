"""archive groups, and where a closed ticket went

NexterPay, 9 September and settled on the 14th: two forum groups per desk,
Active and Closed. A request is moved into the archive 24 hours after it is
closed, the conversation forwarded so it keeps who said what, and the original
topic removed once the copy exists.

Three changes, and one deliberate omission.

`chat_kind` gains 'archive'. On Postgres that is a real enum type and the value
has to be added; on SQLite the column is a VARCHAR and needs nothing. Note the
width: the column was created as VARCHAR(10), sized to "operations". "archive"
is seven characters and fits.

`work_items` gains where the ticket went and when. `archived_at` is what makes
the sweep safe to repeat - it is written only after the copy exists, so a pass
that dies halfway is redone rather than skipped, and the original topic is
deleted only after it is set.

**The omission.** There is no unique index enforcing one archive per
department, and that is not an oversight. Postgres refuses to let a newly added
enum value be *used* in the transaction that adds it, so a partial index whose
WHERE clause names 'archive' would fail here - the same shape of failure as the
duplicate CREATE TYPE that took the bot down on 12 September, and equally
invisible to a test suite running on SQLite. The rule is enforced in
`register_archive_chat`, where it can also name the group that already has the
job.

Revision ID: f2a6c93de140
Revises: e4b81f26aa07
Create Date: 2026-09-15 09:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f2a6c93de140'
down_revision = 'e4b81f26aa07'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Postgres 12+ allows this inside a transaction provided the value is
        # not used in the same one. Nothing below uses it.
        op.execute("ALTER TYPE chat_kind ADD VALUE IF NOT EXISTS 'archive'")

    # batch_alter_table because SQLite cannot ALTER a column in place; on
    # Postgres it is an ordinary ALTER TABLE and costs nothing.
    with op.batch_alter_table("work_items") as batch:
        batch.add_column(
            sa.Column("archive_topic_id", sa.BigInteger(), nullable=True)
        )
        batch.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )

    # The sweep asks one question on a timer: what is closed, old enough, and
    # not yet moved. Without this it is a full scan of every request ever
    # raised, every fifteen minutes, forever.
    op.create_index(
        "ix_work_items_awaiting_archive",
        "work_items",
        ["archived_at", "closed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_items_awaiting_archive", table_name="work_items")
    with op.batch_alter_table("work_items") as batch:
        batch.drop_column("archived_at")
        batch.drop_column("archive_topic_id")

    # 'archive' is left in the enum. Postgres cannot remove a value, and a
    # spare one nobody writes is harmless.
