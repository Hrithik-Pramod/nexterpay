"""our accounts with a supplier, and whose business runs through each

NexterPay, through Jason on 4 October: "BBS is a big supplier, so we have
multiple accounts. BBS number is Nexterpay Number." Every settlement line they
send ends with one - `Nexterpay 5`, `Nexterpay 7` - and until now it was
stored as a label on the settlement and used for nothing.

It is worth more than a label. Their lines carry no reference, so the only
thing to match a payment on was the amount, and two clients sending fifty
million XOF on the same day is ordinary on a desk doing volume. Amount and
account together are far more selective.

Three things about the shape, each from asking rather than from the data.

Numbering is per supplier, so the key is the pair - BBS 5 and SPEX 5 are
unrelated. Accounts are shared, so one account is several rows and the number
narrows a payment rather than identifying it; BBS 1 carries both LuckyStar and
Spayz Category B. And the client is held as a name as well as a link, because
the list names clients not yet registered here - a mapping that could only be
loaded once every client existed would not be loadable at all.

Revision ID: 3f6d82b0ae41
Revises: 7c0ab8e35d19
Create Date: 2026-10-04 16:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '3f6d82b0ae41'
down_revision = '7c0ab8e35d19'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("supplier_code", sa.String(length=8), nullable=False),
        sa.Column("number", sa.String(length=16), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_supplier_accounts_client_id_clients"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_accounts")),
        sa.UniqueConstraint(
            "supplier_code", "number", "client_name", name="uq_supplier_account"
        ),
    )
    op.create_index(
        "ix_supplier_accounts_lookup", "supplier_accounts",
        ["supplier_code", "number"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_accounts_lookup", table_name="supplier_accounts")
    op.drop_table("supplier_accounts")
