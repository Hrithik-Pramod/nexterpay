"""The client's view of their own open requests.

NexterPay's decisions: the whole group's requests rather than one person's,
open ones only, and clients see a coarser set of statuses than staff track.
"""

from __future__ import annotations

import pytest

from app.db.models import Client
from app.domain.enums import WorkItemStatus
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _raise(session, gw, chat, subject):
    return await relay.open_request(
        session, gw, source_chat=chat, subject=subject,
        body=f"about {subject}", raised_by_name="Tom Baker",
    )


async def test_only_open_requests_are_listed(
    session, acme_support, support_ops, operator, gw
):
    first = await _raise(session, gw, acme_support, "Settlement")
    second = await _raise(session, gw, acme_support, "Onboarding")
    await relay.close(session, gw, second, Actor.of(operator))

    listed = await relay.open_requests_for(session, acme_support)
    subjects = [i.subject for i in listed]

    assert "Settlement" in subjects
    assert "Onboarding" not in subjects, "closed requests must not appear"
    assert listed[0].id == first.id


async def test_clients_see_the_simplified_status_wording() -> None:
    """Internal process wording must not reach a customer.

    "Escalated" and "Waiting for Third Party" describe NexterPay's process,
    not the client's situation.
    """
    assert WorkItemStatus.OPEN.client_label == "Received"
    assert WorkItemStatus.WAITING_CLIENT.client_label == "Waiting on you"
    assert WorkItemStatus.ESCALATED.client_label == "In progress"
    assert WorkItemStatus.WAITING_THIRD_PARTY.client_label == "In progress"
    assert WorkItemStatus.COMPLETED.client_label == "Resolved"

    internal_only = {"Escalated", "Waiting for Third Party", "Waiting for Internal Team"}
    shown = {s.client_label for s in WorkItemStatus}
    assert not (shown & internal_only), f"internal wording leaked: {shown & internal_only}"


async def test_the_anchor_lets_a_reply_reach_the_right_request(
    session, acme_support, support_ops, gw
):
    """Tapping a request posts something the client can reply to.

    It reuses the routing that already works rather than inventing a second
    mechanism, so a reply to the anchor resolves to that work item.
    """
    from app.bot.routing import IncomingMessage, ReplyToAcknowledgementStrategy

    item = await _raise(session, gw, acme_support, "Settlement")
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.post_anchor(session, gw, item)
    assert len(gw.messages_to(CLIENT_CHAT)) == before + 1

    # Read the anchor back from the database rather than the fake, because the
    # recorded row is what the routing strategy actually looks up.
    from sqlalchemy import select

    from app.db.models import Message

    rows = await session.execute(
        select(Message)
        .where(Message.work_item_id == item.id, Message.telegram_chat_id == CLIENT_CHAT)
        .order_by(Message.id.desc())
    )
    anchor_id = rows.scalars().first().telegram_message_id
    resolved = await ReplyToAcknowledgementStrategy().resolve(
        session, acme_support,
        IncomingMessage(
            telegram_chat_id=CLIENT_CHAT,
            telegram_message_id=999,
            sender_name="Tom Baker",
            text="any update?",
            reply_to_message_id=anchor_id,
        ),
    )
    assert resolved is not None and resolved.id == item.id


async def test_the_anchor_never_carries_the_supplier_code(
    session, acme_support, support_ops, operator, gw
):
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    item = await _raise(session, gw, acme_support, "Settlement")
    supplier = Client(name="Supplier Pexi", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    await relay.post_anchor(session, gw, item)
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)
    assert "ACME-" in gw.all_text_to(CLIENT_CHAT)
