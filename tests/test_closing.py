"""Closing, and what happens when a client comes back afterwards.

NexterPay's decisions: the client is told, with their original request
repeated back and an optional line on what was done; Business closes
silently; and a reply to a closed request does not reopen it - the person who
closed it is notified and decides.
"""

from __future__ import annotations

import pytest

from app.domain.enums import Department, WorkItemStatus
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _open(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement not received",
        body="We have not received settlement for 3 March.",
        raised_by_name="Tom Baker",
    )


async def test_closure_repeats_the_request_back(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    closure = [m for m in gw.messages_to(CLIENT_CHAT) if "is now resolved" in m][0]
    assert "What you raised on" in closure
    assert "settlement for 3 March" in closure


async def test_closure_includes_what_was_done_when_there_is_something_to_say(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(
        session, gw, item, Actor.of(operator),
        resolution="Refund processed, reference 88213.",
    )

    closure = [m for m in gw.messages_to(CLIENT_CHAT) if "is now resolved" in m][0]
    assert "What we did:" in closure
    assert "88213" in closure


async def test_closure_omits_that_section_when_there_is_not(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    closure = [m for m in gw.messages_to(CLIENT_CHAT) if "is now resolved" in m][0]
    assert "What we did:" not in closure


async def test_business_closes_silently(
    session, acme_support, support_ops, operator, gw
):
    """Business was the stated exception - there the answer is the conclusion."""
    item = await _open(session, gw, acme_support)
    item.department = Department.BUSINESS
    await session.flush()
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.close(session, gw, item, Actor.of(operator))

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert item.status is WorkItemStatus.CLOSED


async def test_a_reply_after_closing_does_not_reopen_it(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item, text="actually it still has not arrived",
        sender_name="Tom Baker", telegram_message_id=555,
    )

    assert item.status is WorkItemStatus.CLOSED, "a client reply must not reopen it"
    # The client is told, rather than left wondering - we invited the reply.
    assert any("already closed" in m for m in gw.messages_to(CLIENT_CHAT))
    # And it reached the person who closed it.
    assert "tg://user" in gw.all_text_to(OPS_CHAT)


async def test_reopening_needs_a_manager(
    session, acme_support, support_ops, operator, manager, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    with pytest.raises(NotAuthorised):
        await relay.reopen(session, gw, item, Actor.of(operator))

    await relay.reopen(session, gw, item, Actor.of(manager))
    assert item.status is WorkItemStatus.IN_PROGRESS
    assert (OPS_CHAT, item.topic_id) in gw.reopened_topics
