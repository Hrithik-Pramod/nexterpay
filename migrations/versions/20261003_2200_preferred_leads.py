"""the person this desk always asks

Jason, 3 October: "also think, there will be key people in some group he
always ask, so we can use lead to identify."

The table already held the people. What it could not say was which of them is
*the* one - so every time the platform wanted to address somebody it had to
offer a list, which is the small friction that makes a flow feel longer than
it is.

Two columns. `is_preferred` marks the person, and `for_currency` narrows it:
"for XOF I always ask Marco at BBS" is how the desk actually works, and a
supplier big enough to quote several corridors usually has somebody different
on each. Currency rather than country, because a rate is quoted per currency
and one XOF contact covers all eight XOF countries.

Nothing is backfilled. A group with several leads and no stated preference has
not told us which one it is, and picking the first alphabetically would be
the platform inventing a working relationship.

Revision ID: 0a7c93d15e26
Revises: f1d8a62b94c7
Create Date: 2026-10-03 22:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0a7c93d15e26'
down_revision = 'f1d8a62b94c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_leads",
        sa.Column("is_preferred", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(
        "group_leads",
        sa.Column("for_currency", sa.String(length=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("group_leads", "for_currency")
    op.drop_column("group_leads", "is_preferred")
