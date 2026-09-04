"""Asking another department, rather than moving a request to it.

NexterPay's answer to "transfer a request between desks", and a better one
than the question. Dragging a live request across means carrying its topic,
its history and the client's view of it into another Operations Group and
hoping all three arrive intact. Opening a fresh request instead reuses
everything that already works, and linking the two means the client still sees
one thread while two desks work on it.

The property this file guards: nothing about it reaches the counterparty. The
client raised one thing. That NexterPay asked Finance about it is internal.
"""

from __future__ import annotations

import pytest

from app.bot.registry import register_operations_chat
from app.domain.enums import Department
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPORT_OPS = -1001000000001
FINANCE_OPS = -1001000000008


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
async def finance_ops(session):
    return await register_operations_chat(
        session, telegram_chat_id=FINANCE_OPS,
        department=Department.FINANCE, title="Finance Operations",
    )


async def _origin(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement not received",
        body="The 3 March settlement has not arrived.", raised_by_name="Tom Baker",
    )


async def test_it_opens_on_the_other_desk_not_this_one(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    origin = await _origin(session, gw, acme_support)
    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
    )

    assert asked.department is Department.FINANCE
    assert asked.operations_chat_id == finance_ops.id
    assert asked.topic_id in gw.topics[FINANCE_OPS]
    assert "Can you confirm the 3 March rate?" in gw.all_text_to(FINANCE_OPS)


async def test_the_client_is_told_nothing(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """The whole point of asking rather than transferring is that the client's
    view does not move. It should not even flicker."""
    origin = await _origin(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before, "the client was written to"
    assert "Finance" not in gw.all_text_to(CLIENT_CHAT)
    assert "confirm the 3 March rate" not in gw.all_text_to(CLIENT_CHAT)


async def test_the_two_are_linked_without_anyone_remembering_to(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """A link nobody makes is not a link, and both halves being visible is
    the entire reason for raising rather than transferring."""
    from app.domain import work_items as wi

    origin = await _origin(session, gw, acme_support)
    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert [i.id for i in await wi.linked_to(session, origin)] == [asked.id]
    assert [i.id for i in await wi.linked_to(session, asked)] == [origin.id]


async def test_it_keeps_the_client_and_the_supplier_filing(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Same client, so the code and the filing structure still hold - which is
    what makes "everything for Acme" answerable across desks."""
    from app.db.models import Client

    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    origin = await _origin(session, gw, acme_support)
    supplier = Client(name="Supplier Pexi Ltd", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, origin, supplier, Actor.of(operator))

    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert asked.client_id == origin.client_id
    assert asked.client_code == "ACME"
    assert asked.supplier_code == "SPEX", "the supplier context did not follow"
    # And still nothing about SPEX reached Acme.
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)


async def test_the_original_is_untouched(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Asking is not transferring. The request stays where it was, with the
    same owner and the same status."""
    origin = await _origin(session, gw, acme_support)
    await relay.claim(session, gw, origin, Actor.of(operator))
    was_status, was_owner, was_ops = origin.status, origin.owner_staff_id, origin.operations_chat_id

    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert origin.status is was_status
    assert origin.owner_staff_id == was_owner
    assert origin.operations_chat_id == was_ops


async def test_someone_who_is_not_staff_cannot_ask(
    session, acme_support, support_ops, finance_ops, gw
):
    origin = await _origin(session, gw, acme_support)
    with pytest.raises(NotAuthorised):
        await relay.open_internal(
            session, gw, origin=origin, department=Department.FINANCE,
            subject="x", body="x", actor=Actor(name="Someone"),
        )


async def test_the_department_it_is_already_on_is_not_offered() -> None:
    """Asking your own desk is not a thing, and offering it invites a request
    that lands back where it started."""
    from app.bot import keyboards as kb

    others = [d for d in Department if d is not Department.SUPPORT]
    labels = [
        b.text for row in kb.department_choices(1, others).inline_keyboard for b in row
    ]
    assert "Support" not in labels
    assert "Finance" in labels
