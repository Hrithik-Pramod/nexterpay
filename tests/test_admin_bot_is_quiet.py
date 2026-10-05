"""What changes when the bot becomes an administrator.

Jason, 4 October, asked for the capture layer to be the primary way the FX
desk works - the bot reading the conversation rather than being invoked. That
needs the bot promoted to administrator in the counterparty groups, and
promotion changes something this platform has quietly depended on since it was
built.

**Under privacy mode, "is a reply" and "is a reply to us" were the same fact.**
Telegram only delivered a bot replies to its own messages, so every message
reaching a handler with a `reply_to` had been aimed at the platform. An
administrator bot receives everything - including two people in a client's
group replying to each other.

The unrouted notice reads "We couldn't match that to one of your requests, so
our Support Team has not been notified". Sent in answer to two colleagues
talking to each other, it is the platform butting into somebody else's
conversation, in front of a client, repeatedly.

That is worse than any message the platform could miss, which is why the check
is before anything is said rather than after.
"""

from __future__ import annotations

from app.bot.routing import replied_to_one_of_ours
from app.db.models import Message
from app.domain import work_items as wi
from app.domain.enums import MessageDirection


async def _ours(session, chat, item, message_id):
    session.add(
        Message(
            work_item_id=item.id,
            direction=MessageDirection.OUTBOUND,
            telegram_chat_id=chat.telegram_chat_id,
            telegram_message_id=message_id,
            sender_name="NexterPay",
            text="Request ACME-1000 has been received.",
        )
    )
    await session.flush()


async def test_a_reply_to_one_of_our_messages_is_ours(
    session, acme_support, support_ops
):
    item = await wi.create_work_item(
        session, source_chat=acme_support, subject="s",
        original_message="m", raised_by_name="Gavs D",
    )
    await _ours(session, acme_support, item, 5001)

    assert await replied_to_one_of_ours(
        session,
        telegram_chat_id=acme_support.telegram_chat_id,
        reply_to_message_id=5001,
    )


async def test_two_people_replying_to_each_other_is_not(
    session, acme_support, support_ops
):
    """The case promotion introduces, and the one that matters.

    Nothing the platform sent has that message id, so it was not aimed at us -
    and the platform says nothing.
    """
    assert not await replied_to_one_of_ours(
        session,
        telegram_chat_id=acme_support.telegram_chat_id,
        reply_to_message_id=9999,
    )


async def test_a_message_that_is_not_a_reply_is_not_ours(
    session, acme_support, support_ops
):
    assert not await replied_to_one_of_ours(
        session,
        telegram_chat_id=acme_support.telegram_chat_id,
        reply_to_message_id=None,
    )


async def test_the_same_message_id_in_another_group_does_not_count(
    session, acme_support, acme_compliance, support_ops
):
    """Telegram message ids are per chat, so 5001 exists in every group. A
    lookup that ignored the chat would answer somebody else's conversation."""
    item = await wi.create_work_item(
        session, source_chat=acme_support, subject="s",
        original_message="m", raised_by_name="Gavs D",
    )
    await _ours(session, acme_support, item, 5001)

    assert not await replied_to_one_of_ours(
        session,
        telegram_chat_id=acme_compliance.telegram_chat_id,
        reply_to_message_id=5001,
    )


def test_the_handler_checks_before_it_speaks() -> None:
    """Structural, because the ordering is the whole point.

    Checking after composing the notice would still work; checking after
    sending it would not, and the difference between the two is one edit by
    somebody who does not know why the check is there.
    """
    import pathlib

    source = pathlib.Path("app/bot/handlers/client.py").read_text(encoding="utf-8")
    body = source[source.index("async def client_reply"):]

    assert body.index("replied_to_one_of_ours(") < body.index("unrouted_notice(")
    assert body.index("replied_to_one_of_ours(") < body.index("await message.reply(notice)")
