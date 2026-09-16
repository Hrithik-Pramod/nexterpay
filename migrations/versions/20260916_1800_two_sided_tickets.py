"""two-sided tickets: a second outside group on a request

Filing Structure and Connected Tickets, section 4. A request can run between a
client and a supplier with NexterPay in the middle, seen as one conversation in
one Operations topic, while each side sees only their own half.

One column. `bridged_chat_id` is the second outside group, null for everything
that is not bridged — which is almost everything, and deliberately so: a
one-sided request behaves exactly as it always has.

What this column costs is worth writing down, because it is the reason section 4
asks for care rather than speed. Until now a request had exactly one outside
group, so sending to the wrong party was impossible by construction — there was
no code path that could do it. A second group removes that guarantee. It is
replaced by an explicit one: `send_client_reply` refuses any destination that is
not one of that request's own groups, and the confirmation names the party
before anything is sent.

Revision ID: d3f8a1b06e57
Revises: b71c4e2f8a93
Create Date: 2026-09-16 18:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd3f8a1b06e57'
down_revision = 'b71c4e2f8a93'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("work_items") as batch:
        batch.add_column(sa.Column("bridged_chat_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_work_items_bridged_chat", "chats", ["bridged_chat_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("work_items") as batch:
        batch.drop_constraint("fk_work_items_bridged_chat", type_="foreignkey")
        batch.drop_column("bridged_chat_id")
