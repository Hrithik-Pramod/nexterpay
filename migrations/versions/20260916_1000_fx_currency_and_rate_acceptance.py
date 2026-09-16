"""the currency a deal is priced in, and the client accepting a rate

NexterPay, 16 September. Two additions, both from the same observation: every
supplier quotes in their own local currency, and all of them quote it the same
way round — Local Currency per 1 USDT.

`currency_code` is what makes a rate mean something. 89.50 on its own is a bare
number; 89.50 INR per USDT is a price. It goes on the deal rather than on the
supplier because the message a client is shown has to carry it.

`fx_rate_accepted` is the client saying yes to a rate before any amounts exist.
It is deliberately not the same event as `fx_client_confirmed`, which is the
client agreeing to an order with figures on it. Those are different promises,
and which one was given is exactly the question a dispute turns on.

No new value is used here, only added — which matters on Postgres, where a
newly added enum value cannot be referenced in the transaction that creates it.

Revision ID: b71c4e2f8a93
Revises: f2a6c93de140
Create Date: 2026-09-16 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b71c4e2f8a93'
down_revision = 'f2a6c93de140'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'fx_rate_accepted'")

    with op.batch_alter_table("fx_orders") as batch:
        batch.add_column(sa.Column("currency_code", sa.String(length=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("fx_orders") as batch:
        batch.drop_column("currency_code")

    # The enum value stays. Postgres cannot remove one, and any event already
    # written with it would become an unreadable row if it could.
