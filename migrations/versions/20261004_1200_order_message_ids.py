"""remember where each side's order message went, so it can be withdrawn

Found live on 4 October, driving a real deal through UAT.

FXACME-1002 was amended from 250,000 EUR down to 200,000 because the supplier
was short. The order message sitting in the client's group kept both its old
figures and its live Confirm button, and tapping it recorded the client as
having confirmed 250,000 - a number that was no longer the order, which they
had agreed in good faith from what was in front of them.

A client agreeing to a figure they were never shown is the worst thing this
platform can do that is not a margin leak, and it was reachable by one tap on
a message nobody had thought to take down.

Two nullable columns holding the Telegram message id of the outstanding order
on each side, so an amendment can withdraw it. Null for every order sent
before today - those messages cannot be withdrawn because we never recorded
where they are, and inventing an id would edit somebody else's message.

Revision ID: 7c0ab8e35d19
Revises: 2b9f41ac6d83
Create Date: 2026-10-04 12:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '7c0ab8e35d19'
down_revision = '2b9f41ac6d83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("client_order_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "fx_orders",
        sa.Column("supplier_order_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fx_orders", "supplier_order_message_id")
    op.drop_column("fx_orders", "client_order_message_id")
