"""an order's figures can change after both sides agreed them

NexterPay, through Jason on 3 October. Asked what happens when a settlement
does not match the orders it covers, the answer was:

    No it should match, or if the supplier does not have enough, the order
    amount may change.

That sentence quietly asked for something the platform could not do. An order
was fixed the moment the client confirmed it - that is what the confirm step
is for - and there was no way back to the figures short of cancelling the deal
and losing its history with it.

One new event type. The amounts themselves need no new columns: they are the
ones already on the order, and the event carries both the old and the new, so
the audit trail answers "what was it before" without a second set of columns
that would have to be kept honest.

Revision ID: f1d8a62b94c7
Revises: e9b347c21f08
Create Date: 2026-10-03 21:00:00

"""
from __future__ import annotations

from alembic import op

revision = 'f1d8a62b94c7'
down_revision = 'e9b347c21f08'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres only; SQLite stores the enum as text and needs nothing. The
    # same shape as the fx_rate_accepted addition in September.
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'fx_order_amended'"
            )


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type without rebuilding it,
    # and rebuilding it would mean rewriting every row in events - an
    # append-only table this project does not touch. Left in place: an unused
    # enum value costs nothing.
    pass
