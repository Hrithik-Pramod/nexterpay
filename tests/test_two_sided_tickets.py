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
# The history says which side
# --------------------------------------------------------------------------

async def test_the_history_names_the_side_a_reply_went_to(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """Section 4: "The history records the direction of every message: which
    side it came from, which side it went to, and who sent it."

    It read "Reply sent to client by peter" whichever side it went to. On a
    two-sided request that is not untidy wording, it is the audit trail saying
    something untrue — and the audit trail is the thing somebody reaches for
    precisely when they are trying to work out what went wrong.
    """
    from app.domain.history import load_events, render_history

    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "can you confirm?",
        to_chat=pexi_supplier,
    )

    history = " ".join(render_history(await load_events(session, item)))
    assert "Reply sent to" in history
    assert "Pexi" in history, f"the history does not say where it went: {history}"


async def test_a_one_sided_history_is_unchanged(
    session, acme_support, support_ops, operator, gw
):
    """Nothing to disambiguate, so nothing added. A request with one
    destination should not start carrying its name on every line."""
    from app.domain.history import load_events, render_history

    item = await _raised(session, gw, acme_support)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "an update"
    )

    history = " ".join(render_history(await load_events(session, item)))
    assert "Reply sent to client by" in history


# --------------------------------------------------------------------------
# Closing tells both
# --------------------------------------------------------------------------

async def test_closing_tells_both_sides(
    session, acme_support, support_ops, manager, pexi_supplier, gw
):
    """NexterPay, 16 September, asked directly: "Supplier — Tell Both".

    The supplier helped with it; leaving them to wonder whether it was ever
    resolved is the same discourtesy as not telling the client.
    """
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.close(session, gw, item, Actor.of(manager))

    assert "resolved" in gw.all_text_to(acme_support.telegram_chat_id)
    assert "resolved" in gw.all_text_to(pexi_supplier.telegram_chat_id)


async def test_each_side_is_closed_with_its_own_reference(
    session, acme_support, support_ops, manager, pexi_supplier, gw
):
    """The leak found on the reply path could arrive by this door just as
    easily, and would be just as invisible."""
    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.close(session, gw, item, Actor.of(manager))

    assert "ACME" not in gw.all_text_to(pexi_supplier.telegram_chat_id)
    assert item.client_reference in gw.all_text_to(acme_support.telegram_chat_id)


async def test_the_closure_notice_does_not_quote_the_client_to_the_supplier(
    session, acme_support, support_ops, manager, pexi_supplier, gw
):
    """The second leak, found the same way as the first — by closing a bridged
    ticket and reading what the supplier actually got.

    The closure notice repeats the original request back, which NexterPay asked
    for and which is right for whoever raised it. On a two-sided request the
    other side did not raise it and has never seen it, so repeating it forwards
    a client's words to a supplier with nobody deciding to. That is section 4's
    first rule, broken by a message written long before section 4 existed.
    """
    item = await relay.open_request(
        session, gw, source_chat=acme_support,
        subject="Settlement missing",
        body="the 14:02 payment to our Dubai account never arrived",
        raised_by_name="Haze",
    )
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    await relay.close(session, gw, item, Actor.of(manager))

    supplier_saw = gw.all_text_to(pexi_supplier.telegram_chat_id)
    assert "resolved" in supplier_saw
    assert "Dubai" not in supplier_saw, (
        f"the client's own words reached the supplier: {supplier_saw}"
    )
    assert "What you raised" not in supplier_saw, (
        "the supplier is being told they raised something they did not"
    )

    # And the client still gets the whole thing.
    client_saw = gw.all_text_to(acme_support.telegram_chat_id)
    assert "Dubai" in client_saw
    assert "What you raised" in client_saw


async def test_a_one_sided_request_still_tells_one_group(
    session, acme_support, support_ops, manager, gw
):
    """The change must not start sending closure notices to groups that were
    never part of anything."""
    item = await _raised(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(manager))

    notices = [
        c for c in gw.calls
        if c.method == "send_message" and "resolved" in c.payload.get("text", "")
    ]
    assert len(notices) == 1


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


# --------------------------------------------------------------------------
# The reference, on every path out — not just the ones we remembered
#
# Found on 20 September, auditing the outbound strings after promising to on
# the 17th and not doing it. This is the third time the words have stayed
# inside and the reference has not:
#
#   16 Sept — a staff reply reached the supplier as ACME-1072.
#   17 Sept — a closure notice quoted the client's words to the supplier.
#   20 Sept — the "already closed" notice, below.
#
# The first two were fixed by writing `reference_for` and applying it where
# the leak had been seen. Nothing looked for the other places, so this one sat
# there through both fixes. Hence the structural test at the bottom.
# --------------------------------------------------------------------------

async def _code(session, chat, code):
    await session.refresh(chat, ["client"])
    chat.client.code = code
    await session.flush()


async def test_a_supplier_replying_to_a_closed_request_sees_their_own_code(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """The leak, reproduced.

    A supplier replies to a two-sided request that has been closed. The notice
    telling them it will not reopen was composed from `item.client_reference`
    while being sent to them — so they were told, in as many words, which
    client the work had been for.
    """
    await _code(session, acme_support, "ACME")
    await _code(session, pexi_supplier, "SPEX")

    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()
    await relay.close(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="any update on this?",
        sender_name="Pexi Desk",
        telegram_message_id=9911,
        from_chat=pexi_supplier,
    )

    seen = gw.all_text_to(pexi_supplier.telegram_chat_id)
    assert "already closed" in seen
    assert "SPEX" in seen
    assert "ACME" not in seen, f"the client's code reached the supplier: {seen}"


async def test_the_client_still_sees_their_own(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """The fix must not simply blank the reference. A notice a counterparty
    cannot match to a request costs somebody a phone call."""
    await _code(session, acme_support, "ACME")
    await _code(session, pexi_supplier, "SPEX")

    item = await _raised(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()
    await relay.close(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="still not right",
        sender_name="Haze",
        telegram_message_id=9912,
        from_chat=acme_support,
    )

    seen = gw.all_text_to(acme_support.telegram_chat_id)
    assert "already closed" in seen
    assert "ACME" in seen


def test_a_function_that_can_write_to_either_side_must_not_build_its_own_reference():
    """The structural guard, written after the third occurrence.

    `item.client_reference` is correct in a function that only ever writes to
    the group that raised the request — the acknowledgement, the claim notice,
    the anchor. It is a leak in any function whose destination can be
    reassigned to the other side, because the reference is then built from one
    party and delivered to the other.

    Those functions are identifiable: they take a `from_chat` or `to_chat`
    and reassign `source` from it. In those, the reference must come from
    `reference_for`, which asks the destination what it is called.

    Checked in the source rather than by behaviour on purpose. A behavioural
    test proves the path somebody thought of; this one fails on a path nobody
    has thought of yet, which is how all three of these arrived.
    """
    import ast
    import pathlib

    source = pathlib.Path("app/services/relay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))]:
        body = ast.get_source_segment(source, fn) or ""
        redirects = "= from_chat or source" in body or "= to_chat or source" in body
        if not redirects:
            continue
        # Comments explaining the rule mention it; code using it is the fault.
        code_lines = [
            line for line in body.splitlines()
            if "client_reference" in line and not line.strip().startswith("#")
        ]
        if code_lines:
            offenders.append(f"  {fn.name}: {code_lines[0].strip()}")

    assert not offenders, (
        "these can write to either side of a two-sided request and build the "
        "reference themselves:\n" + "\n".join(offenders)
        + "\n\nUse `await reference_for(session, item, source)` — it asks the "
          "destination what it is called. See the three dates at the top of "
          "this section."
    )
