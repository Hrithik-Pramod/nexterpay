"""where the payout lands, on the order itself

NexterPay write every settlement line against a country:

    CI - 50250000/583 = 86,192.11   (07/09/2026) Nexterpay 5

while the rate those lines are priced at is quoted per currency - XOF: 583.
The platform held only the currency, which cannot be turned back into a
country: XOF covers eight of them and XAF six, so fourteen of the twenty-nine
countries NexterPay pay into are unreachable from the data we were keeping.

One nullable column. Left null for every deal raised before today, because
picking one of eight countries to backfill would be inventing a fact about
somebody's money. Those deals keep their currency and say nothing about the
country, which is the truth about what was recorded at the time.

Revision ID: e9b347c21f08
Revises: c5e21f84a7b3
Create Date: 2026-10-03 20:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e9b347c21f08'
down_revision = 'c5e21f84a7b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("country_code", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fx_orders", "country_code")
