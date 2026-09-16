"""A request that runs between a client and a supplier.

Filing Structure and Connected Tickets, section 4 — written on 30 August and
the thing every other piece of FX work has been sequenced around since.

This file exists because of one sentence in that document:

    Today the platform can only send a message back to the group a request came
    from, which makes sending to the wrong party impossible by construction. A
    two-sided ticket removes that guarantee, so it is replaced with an explicit
    one.

Everything here tests the replacement. The old guarantee needed no tests — it
was a property of there being nowhere else to send. The new one is code, and
code is only as good as what fails when it is wrong.

The test that matters most is `test_a_message_cannot_reach_a_group_that_is_not
_a_party`. If that one ever goes red, a client's words can reach a supplier,
and nothing else in this suite matters until it is green again.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.bot import keyboards as kb
from app.bot.handlers import bridge
from app.bot.registry import register_client_chat
from app.domain.enums import Department
from app.domain.errors import DomainError
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

OTHER_CLIENT_CHAT = -1002000009999


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def outsider(session, support_ops):
    """A group on the same desk that is not a party to our request."""
    return await register_client_chat(
        session,
        telegram_chat_id=OTHER_CLIENT_CHAT,
        client_name="Somebody Else Ltd",
        department=Department.SUPPORT,
        title="Somebody Else — Support",
    )


async def _raised(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="the 14:02 payment never arrived", raised_by_name="Haze",
    )


# --------------------------------------------------------------------------
# One side, unchanged
# --------------------------------------------------------------------------

async def test_an_ordinary_request_still_has_one_group(
    session, acme_support, support_ops, gw
):
    """The whole design rests on this being true. A one-sided request must
    behave exactly as it always has, or every ticket on the platform is
    carrying the risk of a feature almost none of them use."""
    item = await _raised(session, gw, acme_support)
    chats = await relay.counterparty_chats(session, item)

    assert [c.id for c in chats] == [acme_support.id]
    assert item.bridged_chat_id is None


async def test_the_raising_group_is_always_first(
    session, acme_support, support_ops, pexi_supplier, gw
):
    """So a caller that does not name a destination gets the behaviour every
    one-sided request has always had."""
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    chats = await relay.counterparty_chats(session, item)
    assert chats[0].id == acme_support.id
    assert [c.id for c in chats] == [acme_support.id, pexi_supplier.id]


# --------------------------------------------------------------------------
# The replacement guarantee
# --------------------------------------------------------------------------

async def test_a_message_cannot_reach_a_group_that_is_not_a_party(
    session, acme_support, support_ops, operator, outsider, gw
):
    """The one that matters.

    Until section 4 was built, this was impossible because there was nowhere
    else to address. Now there is, and this is the whole of what stops it.
    """
    item = await _raised(session, gw, acme_support)

    with pytest.raises(DomainError):
        await relay.send_client_reply(
            session, gw, item, Actor.of(operator), "hello",
            to_chat=outsider,
        )

    assert gw.all_text_to(OTHER_CLIENT_CHAT) == "", (
        "a message reached a group that has nothing to do with this request"
    )


async def test_the_refusal_happens_before_anything_is_sent(
    session, acme_support, support_ops, operator, outsider, gw
):
    """Refused, not sent-then-regretted. Telegram will delete a bot's own
    message for 48 hours, but the supplier has already read it."""
    item = await _raised(session, gw, acme_support)
    before = len(gw.calls)

    with pytest.raises(DomainError):
        await relay.send_client_reply(
            session, gw, item, Actor.of(operator), "hello", to_chat=outsider
        )

    assert len(gw.calls) == before, "something was sent before the check ran"


async def test_the_second_side_can_be_replied_to(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "can you confirm the hash?",
        to_chat=pexi_supplier,
    )

    assert "can you confirm the hash?" in gw.all_text_to(
        pexi_supplier.telegram_chat_id
    )


async def test_replying_to_one_side_says_nothing_to_the_other(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """Nothing crosses automatically — section 4's first rule. The supplier
    hearing what we told the client is the failure this whole design exists to
    prevent."""
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "we are chasing the supplier",
        to_chat=acme_support,
    )

    supplier_saw = gw.all_text_to(pexi_supplier.telegram_chat_id)
    assert "chasing the supplier" not in supplier_saw


async def test_the_second_side_never_sees_the_first_sides_code(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """Found live, not here — which is the point of writing it down.

    The first two-sided reply ever sent reached the supplier as
    "ACME-1072 — from peter — …". The words had not crossed; the reference had.
    A supplier who knows the work is for ACME knows whose business it is, and
    that is exactly what the Filing Structure note says stays internal.

    The tests that were supposed to cover this checked that the message body
    did not reach the wrong group. Not one of them looked at what was wrapped
    around it.
    """
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "any update?",
        to_chat=pexi_supplier,
    )

    seen = gw.all_text_to(pexi_supplier.telegram_chat_id)
    assert "any update?" in seen
    assert "ACME" not in seen, f"the client's code reached the supplier: {seen}"


async def test_each_side_is_given_its_own_reference(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """Not a bare number either. A reference a counterparty cannot quote back
    is a reference that costs somebody a phone call."""
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    assert await relay.reference_for(session, item, acme_support) == (
        item.client_reference
    )
    supplier_ref = await relay.reference_for(session, item, pexi_supplier)
    assert str(item.reference) in supplier_ref
    assert "ACME" not in supplier_ref


async def test_the_default_is_still_the_raising_group(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """A caller that does not name a destination must not be given a guess."""
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "an update for you"
    )

    assert "an update for you" in gw.all_text_to(acme_support.telegram_chat_id)
    assert "an update for you" not in gw.all_text_to(
        pexi_supplier.telegram_chat_id
    )


# --------------------------------------------------------------------------
# The confirmation names the party
# --------------------------------------------------------------------------

def test_a_one_sided_request_keeps_the_old_button() -> None:
    labels = [
        b.text for row in kb.confirm_reply(7).inline_keyboard for b in row
    ]
    assert any("Send to Client" in label for label in labels)


def test_a_two_sided_request_names_both() -> None:
    """Section 4: "The confirmation reads 'Send to Acme Payments' or 'Send to
    Supplier Pexi', not simply 'Send'." """
    markup = kb.confirm_reply(
        7, source_name="Acme Payments", bridged_name="Supplier Pexi"
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert any("Acme Payments" in label for label in labels)
    assert any("Supplier Pexi" in label for label in labels)
    assert any("Cancel" in label for label in labels)


def test_the_two_send_buttons_are_different_actions() -> None:
    """Otherwise both would send to the same place and the labels would be a
    lie — which on this screen is the worst thing a label can be."""
    markup = kb.confirm_reply(
        7, source_name="Acme Payments", bridged_name="Supplier Pexi"
    )
    sends = [
        b.callback_data for row in markup.inline_keyboard for b in row
        if b.text.startswith("✉")
    ]
    assert len(sends) == 2
    assert len(set(sends)) == 2


def test_no_tag_buttons_on_a_two_sided_request() -> None:
    """A named contact belongs to one group. "Send and tag Ann" beside two
    destinations is a button whose label does not say which room Ann is in."""

    class _Lead:
        display_name = "Ann"
        telegram_user_id = 99

    markup = kb.confirm_reply(
        7, [_Lead()], source_name="Acme Payments", bridged_name="Supplier Pexi"
    )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert not any("tag" in label for label in labels)


# --------------------------------------------------------------------------
# Opening a request to a second group
# --------------------------------------------------------------------------

async def test_its_own_group_is_not_offered(
    session, acme_support, support_ops, pexi_supplier, gw
):
    """Bridging a request to the group it was raised in would give it two
    identical destinations and a choice where both answers are the same."""
    item = await _raised(session, gw, acme_support)
    options = await bridge.bridgeable_chats(session, item, Department.SUPPORT)

    assert acme_support.id not in {c.id for c in options}
    assert pexi_supplier.id in {c.id for c in options}


async def test_another_desks_groups_are_not_offered(
    session, acme_support, support_ops, acme_compliance, gw
):
    """A Support desk has no business opening a request into a Compliance
    group, for the same reason it cannot raise one there."""
    item = await _raised(session, gw, acme_support)
    options = await bridge.bridgeable_chats(session, item, Department.SUPPORT)

    assert acme_compliance.id not in {c.id for c in options}


async def test_suppliers_come_first(
    session, acme_support, support_ops, pexi_supplier, outsider, gw
):
    """It is nearly always a supplier, and the list is tapped under time
    pressure."""
    item = await _raised(session, gw, acme_support)
    options = await bridge.bridgeable_chats(session, item, Department.SUPPORT)

    assert options[0].is_supplier


def test_a_third_side_is_refused_in_words() -> None:
    """Two at most. A third would have nowhere safe to sit — the design is that
    each side sees only their own half, and half of three is not a thing."""

    class _Item:
        display_reference = "ACME-1042"

    message = bridge.already_bridged(_Item(), "Supplier Pexi")
    assert "Supplier Pexi" in message
    assert "two sides at most" in message


def test_opening_a_request_tells_nobody_outside() -> None:
    """Adding a supplier is a decision about where a request can go, not a
    message. A group receiving "you have been added to ACME-1042" would be
    reading a reference that means nothing to them."""
    import ast
    import inspect

    source = ast.unparse(ast.parse(inspect.getsource(bridge.add_side)))
    assert "send_client_reply" not in source
    assert "gateway" not in source
