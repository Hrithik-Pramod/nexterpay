"""a short, permanent name for each counterparty group

Jason, 19 October: every client and supplier group gets its own Group Code -
BBS Finance = #BBS - so the desk can name a room mid-sentence instead of
picking it from a list, and so that services and routes can be associated with
rooms later ("if Wave/XOF was down, which client groups are actually
affected").

On `chats` rather than `clients`, and that is the decision this migration is
really making. `clients.code` belongs to the company and leads every reference
(FXACME-SPEX-1042); a company can have several rooms, and the question being
built towards is about rooms. BBS Finance and BBS Support are one counterparty
and two groups.

Unique across the whole table rather than per department. A code meaning two
things on two desks is worse than no code: the person typing it cannot tell
which one they got, and the first time they find out is when a client's
figures are in a supplier's group.

Nullable, and null for everything that exists today. A group without a code
behaves exactly as it does now - Jason: "If there is no Group Code, the
existing FX journey just continues as it does now."

Revision ID: 5e2a71c4d8b6
Revises: 3f6d82b0ae41
Create Date: 2026-10-19 10:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '5e2a71c4d8b6'
down_revision = '3f6d82b0ae41'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("group_code", sa.String(length=4), nullable=True))
    # Named explicitly so the downgrade can drop it on Postgres, where an
    # auto-named constraint is a guess at best.
    op.create_unique_constraint("uq_chats_group_code", "chats", ["group_code"])


def downgrade() -> None:
    op.drop_constraint("uq_chats_group_code", "chats", type_="unique")
    op.drop_column("chats", "group_code")
