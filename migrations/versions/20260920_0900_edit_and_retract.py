"""link a relayed message back to the one that produced it

NexterPay, 19 September, testing ACME-1088:

    1) Editing a message after its send out (the message gets edited
       internally but on client group message send out remains the same)
    2) Deleting the message - messages gets deleted internally but on client
       group it still remains.

One column, `messages.origin_message_id`: the message in the Operations Group
that a relayed message came from. Without it the two are unrelated rows and
there is nothing to correct when somebody fixes a typo.

**Only the first of those two can work the way they described it.** Telegram
tells a bot when a message is edited — `edited_message` — and never tells it
when one is deleted. The only deletion update in the Bot API is
`deleted_business_messages`, which applies to connected Telegram Business
accounts, not to groups. So editing is automatic and retraction has to be an
action somebody takes, which is what the Retract button is. The same column
serves both: each needs to find the counterparty's copy of a message.

Nullable and left null for everything already sent. Older replies cannot be
edited — nothing recorded where they came from — and that is honest rather than
awkward: this platform has never known, and inventing a link now would be
guessing at which message produced which.

Revision ID: a7c41e5b90d2
Revises: d3f8a1b06e57
Create Date: 2026-09-20 09:00:00

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a7c41e5b90d2'
down_revision = 'd3f8a1b06e57'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("origin_message_id", sa.BigInteger(), nullable=True),
    )
    # Indexed because the edit handler's only question is "did this message
    # produce a relay", asked on every edit in every Operations Group.
    op.create_index(
        "ix_messages_origin_message_id",
        "messages",
        ["origin_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_origin_message_id", table_name="messages")
    op.drop_column("messages", "origin_message_id")
