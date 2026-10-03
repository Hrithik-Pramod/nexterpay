"""settlements: one payment, many orders, many currencies

NexterPay, through Jason on 3 October, with a week of their real supplier chat
behind it. Their settlements look like this:

    XAF: 3000000/606  = 4,950.495
    XOF: 20100000/585 = 34,358.974
    ≡ 39 309,469 USDT
    51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474

Two orders, two countries, two currencies, one payment, one hash. Until now
the model was one hash on one order, which can represent none of that.

Three tables. `settlements` is the payment - always USDT, because the
currencies belong to the orders underneath rather than to the payment, which
is the opposite of what "settlements can combine many currencies" first
sounded like. `settlement_allocations` is one order's share, carrying the
country, the local amount and the rate exactly as their lines write them.
`settlement_reference_counter` numbers them SET-1000 upward, for the same
reason FX orders have their own counter.

`fx_orders.tx_hash` and `settled_at` are deliberately left alone. Every deal
settled before today has its hash there and nothing pointing at a settlement,
and backfilling one row per historical hash would invent a payment that was
never recorded as one. The columns stay as the record of what was known at
the time, and new settlements write both.

`settlement_allocations.fx_order_id` is unique. NexterPay were asked what
happens when a payment does not match the orders it covers and said it should
match, or the order amount changes - so there is no partial settlement, and a
second allocation against an order is a bug rather than a case.

Revision ID: c5e21f84a7b3
Revises: a7c41e5b90d2
Create Date: 2026-10-03 19:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c5e21f84a7b3'
down_revision = 'a7c41e5b90d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.Integer(), nullable=False),
        sa.Column("chain", sa.String(length=16), nullable=False,
                  server_default="tron"),
        sa.Column("tx_hash", sa.String(length=120), nullable=True),
        sa.Column("amount_usdt", sa.Numeric(24, 8), nullable=True),
        sa.Column("nexterpay_account", sa.String(length=32), nullable=True),
        sa.Column("recorded_by_staff_id", sa.Integer(), nullable=True),
        sa.Column("recorded_by_name", sa.String(length=200), nullable=False,
                  server_default="System"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recorded_by_staff_id"], ["staff.id"],
                                name=op.f("fk_settlements_recorded_by_staff_id_staff")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
    )
    op.create_index(op.f("ix_settlements_reference"), "settlements",
                    ["reference"], unique=True)
    op.create_index(op.f("ix_settlements_tx_hash"), "settlements",
                    ["tx_hash"], unique=False)

    op.create_table(
        "settlement_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("settlement_id", sa.Integer(), nullable=False),
        sa.Column("fx_order_id", sa.Integer(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("local_amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("rate", sa.Numeric(20, 10), nullable=False),
        sa.Column("usdt_amount", sa.Numeric(24, 8), nullable=False),
        sa.ForeignKeyConstraint(
            ["settlement_id"], ["settlements.id"], ondelete="CASCADE",
            name=op.f("fk_settlement_allocations_settlement_id_settlements"),
        ),
        sa.ForeignKeyConstraint(
            ["fx_order_id"], ["fx_orders.id"],
            name=op.f("fk_settlement_allocations_fx_order_id_fx_orders"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlement_allocations")),
        sa.UniqueConstraint("fx_order_id",
                            name=op.f("uq_settlement_allocations_fx_order_id")),
    )
    op.create_index("ix_settlement_allocations_settlement",
                    "settlement_allocations", ["settlement_id"], unique=False)

    op.create_table(
        "settlement_reference_counter",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False,
                  server_default="1000"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlement_reference_counter")),
    )


def downgrade() -> None:
    op.drop_table("settlement_reference_counter")
    op.drop_index("ix_settlement_allocations_settlement",
                  table_name="settlement_allocations")
    op.drop_table("settlement_allocations")
    op.drop_index(op.f("ix_settlements_tx_hash"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_reference"), table_name="settlements")
    op.drop_table("settlements")
