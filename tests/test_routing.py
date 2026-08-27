"""Reply routing.

The mechanism NexterPay agreed: the bot posts a message carrying the reference,
the client replies to it, and the reply resolves to the right work item.

The important negative case is the last test. A client who types a fresh
message resolves to nothing, and that is correct behaviour rather than a bug -
what the bot should *do* about it is still an open question with NexterPay.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.routing import (
    ChainedStrategy,
    IncomingMessage,
    MostRecentOpenItemStrategy,
    ReplyToAcknowledgementStrategy,
    build_strategy,
)
from app.db.models import Message
from app.domain import work_items as wi
from app.domain.enums import MessageDirection


async def _item_with_ack(session, chat, *, subject, ack_message_id):
    """Create a work item and record the acknowledgement the bot posted into
    the client group - the anchor a client reply will point at."""
    item = await wi.create_work_item(
        session,
        source_chat=chat,
        subject=subject,
        original_message=subject,
        raised_by_name="Tom Baker",
    )
    session.add(
        Message(
            work_item_id=item.id,
            direction=MessageDirection.OUTBOUND,
            telegram_chat_id=chat.telegram_chat_id,
            telegram_message_id=ack_message_id,
            sender_name="NexterPay Operations",
            text=f"Request {item.display_reference} received.",
        )
    )
    await session.flush()
    return item


async def test_reply_resolves_to_the_right_item(session, acme_support, support_ops):
    first = await _item_with_ack(session, acme_support, subject="Settlement", ack_message_id=100)
    second = await _item_with_ack(session, acme_support, subject="Login", ack_message_id=200)

    strategy = ReplyToAcknowledgementStrategy()
    incoming = IncomingMessage(
        telegram_chat_id=acme_support.telegram_chat_id,
        telegram_message_id=301,
        sender_name="Tom Baker",
        text="Here is the confirmation.",
        reply_to_message_id=100,
    )

    resolved = await strategy.resolve(session, acme_support, incoming)
    assert resolved is not None
    assert resolved.id == first.id
    assert resolved.id != second.id


async def test_reply_to_unknown_message_resolves_to_nothing(session, acme_support, support_ops):
    await _item_with_ack(session, acme_support, subject="Settlement", ack_message_id=100)

    strategy = ReplyToAcknowledgementStrategy()
    incoming = IncomingMessage(
        telegram_chat_id=acme_support.telegram_chat_id,
        telegram_message_id=302,
        sender_name="Tom Baker",
        reply_to_message_id=999,  # a message the platform never sent
    )

    assert await strategy.resolve(session, acme_support, incoming) is None


async def test_fresh_message_resolves_to_nothing(session, acme_support, support_ops):
    """A client typing rather than replying. Correct outcome, open question as
    to what the bot should then do."""
    await _item_with_ack(session, acme_support, subject="Settlement", ack_message_id=100)

    strategy = ReplyToAcknowledgementStrategy()
    incoming = IncomingMessage(
        telegram_chat_id=acme_support.telegram_chat_id,
        telegram_message_id=303,
        sender_name="Tom Baker",
        text="Any update on this?",
        reply_to_message_id=None,
    )

    assert await strategy.resolve(session, acme_support, incoming) is None


async def test_fallback_strategy_attaches_to_most_recent(session, acme_support, support_ops):
    await _item_with_ack(session, acme_support, subject="Settlement", ack_message_id=100)
    newest = await _item_with_ack(session, acme_support, subject="Login", ack_message_id=200)

    chained = ChainedStrategy(ReplyToAcknowledgementStrategy(), MostRecentOpenItemStrategy())
    incoming = IncomingMessage(
        telegram_chat_id=acme_support.telegram_chat_id,
        telegram_message_id=304,
        sender_name="Tom Baker",
        text="Any update?",
    )

    resolved = await chained.resolve(session, acme_support, incoming)
    assert resolved is not None and resolved.id == newest.id


def test_configured_strategy_is_the_agreed_one():
    from app.config import Settings

    assert Settings().reply_routing_strategy == "reply_to_ack"
    assert isinstance(build_strategy("reply_to_ack"), ReplyToAcknowledgementStrategy)


def test_unknown_strategy_fails_loudly():
    with pytest.raises(ValueError):
        build_strategy("guess")


async def test_client_reply_is_not_swallowed_by_the_staff_topic_handler(
    session, acme_support, monkeypatch
):
    """A client replying to the bot must reach the client router.

    Regression, found in UAT. `topic_message` is filtered on
    message_thread_id, which reads as "this is a forum topic" but is not:
    Telegram sets message_thread_id on ANY reply in a supergroup, including
    client groups with topics switched off.

    So a client replying to the bot's "describe your request" prompt matched
    the staff handler, which is registered first. `_resolve` returned None -
    correctly, since a client group has no staff - and the handler returned.
    aiogram counted the update as handled and the client router never ran.

    The symptom was the worst kind: the reply arrived, no ticket was created,
    nothing was logged, and no error was raised anywhere. It cost most of a
    day to find.
    """
    import contextlib

    from aiogram.dispatcher.event.bases import SkipHandler

    from app.bot.handlers import staff as staff_handlers

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(staff_handlers, "session_scope", fake_scope)

    reply_in_client_group = SimpleNamespace(
        chat=SimpleNamespace(id=acme_support.telegram_chat_id, type="supergroup"),
        message_thread_id=8842,          # set by Telegram because it is a reply
        text="the payment still has not arrived",
        caption=None,
        from_user=SimpleNamespace(id=8230258656, full_name="Charley"),
    )

    with pytest.raises(SkipHandler):
        await staff_handlers.topic_message(reply_in_client_group)


async def test_unregistered_group_is_skipped_not_swallowed(session, monkeypatch):
    import contextlib

    from aiogram.dispatcher.event.bases import SkipHandler

    from app.bot.handlers import staff as staff_handlers

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(staff_handlers, "session_scope", fake_scope)

    stranger = SimpleNamespace(
        chat=SimpleNamespace(id=-1009999999999, type="supergroup"),
        message_thread_id=1,
        text="hello",
        caption=None,
        from_user=SimpleNamespace(id=1, full_name="Nobody"),
    )
    with pytest.raises(SkipHandler):
        await staff_handlers.topic_message(stranger)
