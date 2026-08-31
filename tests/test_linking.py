"""Connected tickets.

Section 3 of the filing structure document: any two tickets can be tied
together where the same problem produced one from a client and another with a
supplier. The promises made there are that a link is symmetric, that it shows
in both topics and both histories, and that it does not merge anything.

The promise NOT made there, and the one this file guards hardest, is that a
link is internal. The other ticket's reference can belong to a different
client or carry a supplier code, so nothing about it may reach a counterparty
group.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Client, WorkItemLink
from app.domain import work_items as wi
from app.domain.enums import EventType
from app.domain.errors import DomainError, NotAuthorised
from app.domain.history import load_events, render_history
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPLIER_CHAT = -1003000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _two(session, gw, acme_support, pexi_supplier, operator):
    """One ticket from the client, one raised with the supplier.

    The exact case the document describes: the same underlying problem, seen
    from both ends.
    """
    theirs = await relay.open_request(
        session, gw, source_chat=acme_support,
        subject="Settlement not received",
        body="The 3 March settlement has not arrived.",
        raised_by_name="Tom Baker",
    )
    ours = await relay.open_outbound(
        session, gw, counterparty_chat=pexi_supplier,
        subject="Missing settlement file",
        body="We are missing the 3 March file from you.",
        actor=Actor.of(operator),
    )
    return theirs, ours


async def test_a_link_is_visible_from_both_sides(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    assert [i.id for i in await wi.linked_to(session, theirs)] == [ours.id]
    assert [i.id for i in await wi.linked_to(session, ours)] == [theirs.id]


async def test_it_appears_in_both_histories(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    """Either ticket, read on its own, has to show that the other exists."""
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    theirs_history = "\n".join(render_history(await load_events(session, theirs)))
    ours_history = "\n".join(render_history(await load_events(session, ours)))

    assert f"Linked to {ours.display_reference}" in theirs_history
    assert f"Linked to {theirs.display_reference}" in ours_history


async def test_linking_the_other_way_round_is_the_same_link(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    """The bug this guards against is a duplicate, not an error.

    Two people, in two topics, linking the same pair in opposite directions is
    an ordinary Tuesday. If that produced two rows, the other ticket would
    appear twice in the header and in the list, and nobody would work out why.
    """
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    with pytest.raises(DomainError):
        await relay.link(session, gw, ours, theirs, Actor.of(operator))

    rows = (await session.execute(select(WorkItemLink))).scalars().all()
    assert len(rows) == 1
    assert [i.id for i in await wi.linked_to(session, theirs)] == [ours.id]


async def test_a_ticket_cannot_be_linked_to_itself(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Not received.", raised_by_name="Tom Baker",
    )
    with pytest.raises(DomainError):
        await relay.link(session, gw, item, item, Actor.of(operator))


async def test_linking_does_not_merge_anything(
    session, acme_support, pexi_supplier, support_ops, operator, senior, gw
):
    """Each keeps its own owner, status and conversation."""
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.claim(session, gw, theirs, Actor.of(operator))
    await relay.link(session, gw, theirs, ours, Actor.of(senior))

    assert theirs.owner_staff_id == operator.id
    assert ours.owner_staff_id is None
    assert theirs.status is not ours.status


async def test_nothing_about_a_link_reaches_either_counterparty(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    """The property that matters most, and the reason this is not a client feature.

    The client raised one ticket. The other belongs to a supplier and its
    reference names that supplier. If the link were announced outward, a client
    could read which supplier their problem was filed against - which is
    exactly what the client-facing reference exists to prevent.
    """
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    supplier = await session.get(Client, pexi_supplier.client_id)
    supplier.code = "SPEX"
    await session.flush()

    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    before_client = len(gw.messages_to(CLIENT_CHAT))
    before_supplier = len(gw.messages_to(SUPPLIER_CHAT))

    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    assert len(gw.messages_to(CLIENT_CHAT)) == before_client, "the client was told"
    assert len(gw.messages_to(SUPPLIER_CHAT)) == before_supplier, "the supplier was told"
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)
    assert "ACME" not in gw.all_text_to(SUPPLIER_CHAT)

    # And it did land where it belongs.
    assert "Linked to" in gw.all_text_to(OPS_CHAT)


async def test_the_header_of_both_tickets_names_the_other(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    assert ours.display_reference in gw.current_text(theirs.header_message_id)
    assert theirs.display_reference in gw.current_text(ours.header_message_id)


async def test_an_unlinked_ticket_has_no_linked_line(
    session, acme_support, support_ops, operator, gw
):
    """Most tickets are linked to nothing and should not carry a spare line."""
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Not received.", raised_by_name="Tom Baker",
    )
    assert "Linked:" not in relay.header_text(item, "Acme Payments")


async def test_unlinking_removes_it_from_both_sides_but_not_from_the_record(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))
    assert await relay.unlink(session, gw, theirs, ours, Actor.of(operator)) is True

    assert await wi.linked_to(session, theirs) == []
    assert await wi.linked_to(session, ours) == []

    kinds = [e.event_type for e in await load_events(session, theirs)]
    assert EventType.TICKETS_LINKED in kinds, "the link was erased from history"
    assert EventType.TICKETS_UNLINKED in kinds


async def test_unlinking_two_tickets_that_were_never_linked_says_so(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    assert await relay.unlink(session, gw, theirs, ours, Actor.of(operator)) is False


async def test_a_closed_ticket_can_still_be_linked(
    session, acme_support, pexi_supplier, support_ops, operator, manager, gw
):
    """"This is the same thing we closed last month" is a link worth having.

    Refusing it would mean a connection can only be recorded while both
    tickets are live, which is rarely when anyone spots it.
    """
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.close(session, gw, theirs, Actor.of(manager))

    await relay.link(session, gw, theirs, ours, Actor.of(operator))
    assert [i.id for i in await wi.linked_to(session, ours)] == [theirs.id]


async def test_someone_who_is_not_staff_cannot_link(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    with pytest.raises(NotAuthorised):
        await relay.link(session, gw, theirs, ours, Actor(name="Someone"))
    assert await wi.linked_to(session, theirs) == []


# --------------------------------------------------------------------------
# The button, which is how this will actually be used
# --------------------------------------------------------------------------


def _fake_query():
    """Enough of a CallbackQuery to drive `_apply`, and no more permissive."""
    from types import SimpleNamespace

    captured: dict = {}

    async def edit_reply_markup(reply_markup=None, **kwargs):
        captured["markup"] = reply_markup

    async def answer(text, **kwargs):
        if "message_thread_id" in kwargs:
            raise TypeError("Message.answer() already sets message_thread_id")
        return SimpleNamespace(message_id=1)

    return SimpleNamespace(
        message=SimpleNamespace(
            message_thread_id=55, edit_reply_markup=edit_reply_markup, answer=answer
        ),
        captured=captured,
    )


async def test_the_picker_offers_the_other_ticket_and_not_this_one(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    from app.bot import deps
    from app.bot.handlers import staff as staff_handlers

    deps.set_gateway(gw)
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)

    query = _fake_query()
    await staff_handlers._apply(session, query, "link", None, theirs, Actor.of(operator), None)

    labels = [b.text for row in query.captured["markup"].inline_keyboard for b in row]
    assert any(ours.display_reference in text for text in labels)
    assert not any(theirs.display_reference in text for text in labels), (
        "a ticket was offered as a link to itself"
    )


async def test_the_picker_offers_an_existing_link_for_removal_instead(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    """Once linked, the same button is how you unlink - not a second one."""
    from app.bot import deps
    from app.bot.handlers import staff as staff_handlers

    deps.set_gateway(gw)
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    await relay.link(session, gw, theirs, ours, Actor.of(operator))

    query = _fake_query()
    await staff_handlers._apply(session, query, "link", None, theirs, Actor.of(operator), None)

    rows = query.captured["markup"].inline_keyboard
    for row in rows:
        for button in row:
            if ours.display_reference in button.text:
                assert button.text.startswith("✕"), "no way to undo it"
                assert "unlink" in button.callback_data
                return
    raise AssertionError("the linked ticket was not on the keyboard at all")


async def test_the_button_sends_nothing_to_a_counterparty(
    session, acme_support, pexi_supplier, support_ops, operator, gw
):
    from app.bot import deps
    from app.bot.handlers import staff as staff_handlers

    deps.set_gateway(gw)
    theirs, ours = await _two(session, gw, acme_support, pexi_supplier, operator)
    before = len(gw.messages_to(CLIENT_CHAT)), len(gw.messages_to(SUPPLIER_CHAT))

    await staff_handlers._apply(
        session, _fake_query(), "dolink", str(ours.id), theirs, Actor.of(operator), None
    )

    assert [i.id for i in await wi.linked_to(session, theirs)] == [ours.id]
    assert (len(gw.messages_to(CLIENT_CHAT)), len(gw.messages_to(SUPPLIER_CHAT))) == before


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("ACME-SPEX-1042", 1042),
        ("ACME-1042", 1042),
        ("#1042", 1042),
        ("1042", 1042),
        ("  acme-1042  ", 1042),
        ("ACME", None),
        ("", None),
    ],
)
def test_a_reference_is_read_in_any_form_someone_might_copy_it(typed, expected) -> None:
    """All four forms name the same ticket, and people copy whichever they see."""
    assert wi.parse_reference(typed) == expected
