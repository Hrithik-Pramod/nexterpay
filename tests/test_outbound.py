"""Requests NexterPay raise with a client or supplier.

The mirror of a client raising one. Sam called it needed across departments,
so it has to be as safe as the inbound path: it creates a work item and
writes into an outside group, which makes it the second route by which
something staff wrote reaches a counterparty.
"""

from __future__ import annotations

import pytest

from app.db.models import Client
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPLIER_CHAT = -1003000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def test_it_opens_a_real_request_and_sends_it(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_outbound(
        session, gw, counterparty_chat=acme_support,
        subject="Settlement discrepancy",
        body="We have found a discrepancy in the 3 March settlement file.",
        actor=Actor.of(operator),
    )

    assert item.topic_id is not None, "a topic should have been opened"
    assert item.raised_by_name == operator.display_name

    outbound = gw.all_text_to(CLIENT_CHAT)
    assert "discrepancy in the 3 March" in outbound
    assert item.client_reference in outbound
    assert "Reply to this message to respond" in outbound

    # And the team can see who raised it and with whom.
    topic = gw.all_text_to(OPS_CHAT)
    assert f"Raised by {operator.display_name}" in topic
    assert "Acme Payments" in topic


async def test_the_direction_is_recorded(
    session, acme_support, support_ops, operator, gw
):
    """"What we raised" and "what they raised" must stay separable."""
    ours = await relay.open_outbound(
        session, gw, counterparty_chat=acme_support, subject="Chasing",
        body="Any update?", actor=Actor.of(operator),
    )
    theirs = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Not received.", raised_by_name="Tom Baker",
    )

    assert ours.raised_by_us is True
    assert theirs.raised_by_us is False


async def test_the_counterparty_is_not_told_it_is_a_logged_request(
    session, acme_support, support_ops, operator, gw
):
    """The acknowledgement wording is wrong in this direction.

    "Request X has been logged with our Support team" is nonsense when we are
    the ones raising it with them.
    """
    await relay.open_outbound(
        session, gw, counterparty_chat=acme_support, subject="Chasing",
        body="Any update on the settlement?", actor=Actor.of(operator),
    )
    assert "has been logged with our" not in gw.all_text_to(CLIENT_CHAT)


async def test_a_reply_from_them_reaches_the_right_request(
    session, acme_support, support_ops, operator, gw
):
    """It reuses the routing that already works rather than a second one."""
    from sqlalchemy import select

    from app.bot.routing import IncomingMessage, ReplyToAcknowledgementStrategy
    from app.db.models import Message

    item = await relay.open_outbound(
        session, gw, counterparty_chat=acme_support, subject="Chasing",
        body="Any update?", actor=Actor.of(operator),
    )
    rows = await session.execute(
        select(Message)
        .where(Message.work_item_id == item.id, Message.telegram_chat_id == CLIENT_CHAT)
        .order_by(Message.id.desc())
    )
    anchor = rows.scalars().first()

    resolved = await ReplyToAcknowledgementStrategy().resolve(
        session, acme_support,
        IncomingMessage(
            telegram_chat_id=CLIENT_CHAT, telegram_message_id=4242,
            sender_name="Tom Baker", text="looking into it",
            reply_to_message_id=anchor.telegram_message_id,
        ),
    )
    assert resolved is not None and resolved.id == item.id


async def test_it_works_towards_a_supplier_too(
    session, pexi_supplier, support_ops, operator, gw
):
    """Suppliers are the reason this was asked for."""
    item = await relay.open_outbound(
        session, gw, counterparty_chat=pexi_supplier,
        subject="API returning 500s", body="Your sandbox is failing since 09:00.",
        actor=Actor.of(operator),
    )
    assert item.raised_by_us is True
    assert "sandbox is failing" in gw.all_text_to(SUPPLIER_CHAT)
    assert gw.messages_to(CLIENT_CHAT) == [], "the wrong counterparty was written to"


async def test_someone_who_is_not_staff_cannot_raise_one(
    session, acme_support, support_ops, gw
):
    with pytest.raises(NotAuthorised):
        await relay.open_outbound(
            session, gw, counterparty_chat=acme_support, subject="x",
            body="x", actor=Actor(name="Someone"),
        )
    assert gw.messages_to(CLIENT_CHAT) == []


async def test_the_supplier_code_still_never_reaches_them(
    session, acme_support, support_ops, operator, gw
):
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    item = await relay.open_outbound(
        session, gw, counterparty_chat=acme_support, subject="Chasing",
        body="Any update?", actor=Actor.of(operator),
    )
    supplier = Client(name="Supplier Pexi Ltd", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    await relay.post_anchor(session, gw, item)
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)


def test_the_composer_runs_before_the_topic_catch_all() -> None:
    """Same trap as the client-reply bug and the broadcast composer."""
    from app.bot.handlers import outbound

    names = [h.callback.__name__ for h in outbound.router.message.handlers]
    assert "capture" in names
