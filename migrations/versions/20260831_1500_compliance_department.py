"""compliance and risk department

Adds the fifth department. Requests here come from suppliers, clients and
NexterPay themselves - KYC requests and compliance notes for action - filed
under the same Client / Supplier / Ticket structure as everything else, with
the standard statuses and the full set of buttons.

On Postgres `department` is a real enum type, so the new value has to be added
to it. On SQLite the column is a plain VARCHAR and needs nothing.

Note the length: the column was created as VARCHAR(11) on SQLite, sized to
"development". "compliance" is 10 characters and fits. A longer department
name in future would not, and would need the column widening first.

Revision ID: e7f3a91c04d8
Revises: c41d8e0b6a52
Create Date: 2026-08-31 15:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = 'e7f3a91c04d8'
down_revision = 'c41d8e0b6a52'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Postgres 12+ allows this inside a transaction as long as the new
        # value is not used in the same one. This migration only adds it.
        op.execute("ALTER TYPE department ADD VALUE IF NOT EXISTS 'compliance'")


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type. Leaving it costs
    # nothing; removing it would mean recreating the type and rewriting every
    # row in three tables that reference it.
    pass
