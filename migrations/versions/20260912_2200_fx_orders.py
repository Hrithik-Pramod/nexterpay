"""fx orders, with the two sides of a deal kept apart

NexterPay specified the FX route on 12 September. A deal has a client on one
side and a supplier on the other with NexterPay in the middle, and every
figure exists twice: the client is quoted our rate, the supplier quotes us
theirs, the client pays one amount and the supplier receives another. The
difference is the margin.

So the two halves are separate columns rather than one set of figures with a
side flag. A function holding a deal cannot read "the rate" - it has to name
client_rate or supplier_rate, which means a leak needs somebody to write the
wrong field rather than merely to forget which one they were holding. That is
the whole reason for the shape of this table.

The deal is anchored to work items rather than replacing them: the client half
is an ordinary request in the client's group, the supplier half an ordinary
request in theirs. Every existing guarantee about what a counterparty can see
therefore still applies, and this adds the figures rather than a second way
for messages to travel.

Revision ID: e4b81f26aa07
Revises: c7e2a9b41d63
Create Date: 2026-09-12 22:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'e4b81f26aa07'
down_revision = 'c7e2a9b41d63'
branch_labels = None
depends_on = None


FX_STATUS_VALUES = (
    "rate_requested",
    "rate_quoted",
    "rate_rejected",
    "awaiting_client_confirmation",
    "awaiting_supplier_acceptance",
    "awaiting_settlement",
    "awaiting_receipt",
    "closed",
)


def _status_type(is_postgres: bool):
    """The column type for `status`, which differs by dialect for one reason.

    On Postgres an enum is a real type that has to exist before the table that
    uses it. Creating it explicitly and *also* passing a plain `sa.Enum` to
    `create_table` means SQLAlchemy emits a second CREATE TYPE for the same
    name, and the migration dies with DuplicateObjectError - which is exactly
    how this one failed the first time it met a real database.

    `create_type=False` says "this type already exists, just reference it", so
    only the explicit create runs.

    On SQLite there is no enum type at all; it becomes a VARCHAR with a check
    constraint and nothing is created separately. Which is also why the whole
    test suite passed while this was broken: the tests run on SQLite and never
    execute the branch that failed.
    """
    if is_postgres:
        return postgresql.ENUM(
            *FX_STATUS_VALUES, name="fx_order_status", create_type=False
        )
    return sa.Enum(*FX_STATUS_VALUES, name="fx_order_status")

# The new event types. Separate from STATUS_CHANGED because these carry money:
# "the rate was set to 1.1642 by Gavin" is the line somebody reads back six
# weeks later when a client disputes what was agreed, and a generic status
# change cannot hold it.
FX_EVENT_TYPES = (
    "fx_rate_requested",
    "fx_supplier_quoted",
    "fx_supplier_rate_rejected",
    "fx_rate_quoted",
    "fx_rate_rejected",
    "fx_order_created",
    "fx_client_confirmed",
    "fx_supplier_accepted",
    "fx_hash_recorded",
    "fx_receipt_confirmed",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # checkfirst so a re-run after a partial failure does not trip on a
        # type that is already there.
        postgresql.ENUM(*FX_STATUS_VALUES, name="fx_order_status").create(
            bind, checkfirst=True
        )

    op.create_table(
        "fx_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reference", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("client_code", sa.String(length=4), nullable=True),
        sa.Column("supplier_code", sa.String(length=4), nullable=True),
        sa.Column("client_work_item_id", sa.Integer(), nullable=False),
        sa.Column("supplier_work_item_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            _status_type(is_postgres),
            nullable=False,
            server_default="rate_requested",
        ),

        # The client's half.
        #
        # account_name is free format and it is what the counterparty sees on
        # the order. For a client it is their business name; for a supplier it
        # is our account code with them - "Nexterpay7" - which is what keeps
        # the client's identity off the supplier's side of the deal.
        sa.Column("client_account_name", sa.String(length=120), nullable=True),
        sa.Column("client_rate", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("client_pays", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("client_pays_currency", sa.String(length=8), nullable=True),
        sa.Column("client_receives", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("client_receives_currency", sa.String(length=8), nullable=True),
        sa.Column("client_confirmed_at", sa.DateTime(timezone=True), nullable=True),

        # The supplier's half, which never crosses.
        sa.Column("supplier_account_name", sa.String(length=120), nullable=True),
        sa.Column("supplier_rate", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("supplier_pays", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("supplier_pays_currency", sa.String(length=8), nullable=True),
        sa.Column("supplier_receives", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("supplier_receives_currency", sa.String(length=8), nullable=True),
        sa.Column("supplier_confirmed_at", sa.DateTime(timezone=True), nullable=True),

        # Settlement. Tron only for now; Ethereum is phase two. The chain is
        # stored rather than assumed so the explorer link never has to be
        # guessed from the shape of the hash.
        sa.Column("tx_hash", sa.String(length=120), nullable=True),
        sa.Column("chain", sa.String(length=16), nullable=False, server_default="tron"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),

        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], name="fk_fx_orders_client"),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["clients.id"], name="fk_fx_orders_supplier"
        ),
        sa.ForeignKeyConstraint(
            ["client_work_item_id"], ["work_items.id"], name="fk_fx_orders_client_wi"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_work_item_id"], ["work_items.id"], name="fk_fx_orders_supplier_wi"
        ),
    )
    op.create_index("ix_fx_orders_reference", "fx_orders", ["reference"], unique=True)
    op.create_index("ix_fx_orders_open", "fx_orders", ["status"])
    op.create_index("ix_fx_orders_client", "fx_orders", ["client_id"])

    # Separate from the work item counter so FX orders run 1000, 1001, 1002
    # rather than taking every third number from a shared pool and looking as
    # though something has gone missing.
    op.create_table(
        "fx_reference_counter",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False, server_default="1000"),
        sa.PrimaryKeyConstraint("id"),
    )

    if is_postgres:
        for value in FX_EVENT_TYPES:
            # ADD VALUE cannot run inside a transaction block on older servers,
            # and IF NOT EXISTS makes the whole thing safe to re-run.
            op.execute(f"ALTER TYPE event_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_index("ix_fx_orders_client", table_name="fx_orders")
    op.drop_index("ix_fx_orders_open", table_name="fx_orders")
    op.drop_index("ix_fx_orders_reference", table_name="fx_orders")
    op.drop_table("fx_reference_counter")
    op.drop_table("fx_orders")

    if op.get_bind().dialect.name == "postgresql":
        postgresql.ENUM(*FX_STATUS_VALUES, name="fx_order_status").drop(
            op.get_bind(), checkfirst=True
        )

    # The event_type values are deliberately left in place. Postgres cannot
    # drop a value from an enum, and any events already written with one would
    # become unreadable rows if it could. Spare values nobody emits are
    # harmless.
