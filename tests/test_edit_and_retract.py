"""Correcting and taking back a message that has already been sent.

NexterPay, 19 September, testing ACME-1088:

    1) Editing a message after its send out (the message gets edited
       internally but on client group message send out remains the same)
    2) Deleting the message - messages gets deleted internally but on client
       group it still remains.

The two look like one feature and are not, and the difference is Telegram's
rather than ours. **An edit is an event**: the Bot API sends `edited_message`,
so the correction can be carried outward on its own. **A deletion is not.** The
only deletion update in the API is `deleted_business_messages`, which applies
to connected Telegram Business accounts and not to groups — a bot in a group is
never told a message was deleted, so there is nothing to react to and no amount
of work produces one.

So editing is automatic and retraction is a button, and the tests below are
split the same way. The second half also pins the 48-hour limit, which is
Telegram's: past it a message cannot be deleted by anyone, and the honest
outcome is to mark it withdrawn rather than to report a removal that did not
happen.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.base import utcnow
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001

ORIGIN = 4242  # the message somebody typed in the topic


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _open(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="We have not received settlement for 3 March.",
        raised_by_name="Tom Baker",
    )


async def _sent(session, gw, chat, operator, text="Paid at 14:02.", origin=ORIGIN):
    item = await _open(session, gw, chat)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), text, origin_message_id=origin,
    )
    return item


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------

async def test_an_edit_reaches_the_client(
    session, acme_support, support_ops, operator, gw
):
    item = await _sent(session, gw, acme_support, operator)

    corrected = await relay.edit_relayed_reply(
        session, gw, item, ORIGIN, "Paid at 15:02 — apologies, wrong time.",
    )

    assert corrected == 1
    edits = [t for texts in gw.edits.values() for t in texts]
    assert any("15:02" in t for t in edits)


async def test_a_corrected_message_keeps_its_shape(
    session, acme_support, support_ops, operator, gw
):
    """Rebuilt rather than patched, so it looks like any other reply — same
    marker, same header, same invitation. A correction that arrives in a
    different shape reads as a different kind of message."""
    item = await _sent(session, gw, acme_support, operator)
    await relay.edit_relayed_reply(session, gw, item, ORIGIN, "Corrected.")

    edited = [t for texts in gw.edits.values() for t in texts][-1]
    assert edited.startswith(relay.MARK_RESPONSE)
    assert item.client_reference in edited
    assert relay.REPLY_HINT in edited


async def test_editing_something_that_never_went_out_changes_nothing(
    session, acme_support, support_ops, operator, gw
):
    """An internal note has no copy anywhere, and never did. Returning zero is
    what lets the handler say so instead of going quiet — which is precisely
    what was reported in the first place."""
    item = await _open(session, gw, acme_support)
    before = len(gw.edits)

    assert await relay.edit_relayed_reply(session, gw, item, 999, "nope") == 0
    assert len(gw.edits) == before


async def test_a_reply_sent_before_this_existed_cannot_be_edited(
    session, acme_support, support_ops, operator, gw
):
    """Honest rather than awkward. Older replies were recorded without knowing
    which message produced them, so there is nothing to find — and inventing a
    link now would be guessing at which message produced which."""
    item = await _open(session, gw, acme_support)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Sent the old way.",
    )
    assert await relay.edit_relayed_reply(session, gw, item, ORIGIN, "x") == 0


async def test_the_record_is_updated_too(
    session, acme_support, support_ops, operator, gw
):
    """Otherwise the archive would keep forwarding the original wording, and
    the history would disagree with what the client is looking at."""
    item = await _sent(session, gw, acme_support, operator)
    await relay.edit_relayed_reply(session, gw, item, ORIGIN, "The corrected words.")

    copies = await relay.relayed_copies_of(session, ORIGIN)
    assert "The corrected words." in copies[0].text


# --------------------------------------------------------------------------
# Retracting
# --------------------------------------------------------------------------

async def test_retracting_removes_the_client_s_copy(
    session, acme_support, support_ops, operator, gw
):
    item = await _sent(session, gw, acme_support, operator)

    deleted, withdrawn = await relay.retract_relayed_reply(session, gw, item, ORIGIN)

    assert (deleted, withdrawn) == (1, 0)
    assert any(chat == CLIENT_CHAT for chat, _ in gw.deleted)


async def test_after_48_hours_it_is_marked_withdrawn_instead(
    session, acme_support, support_ops, operator, gw
):
    """Telegram's limit, not ours: "A message can only be deleted if it was
    sent less than 48 hours ago." Past it the copy cannot be removed by anyone,
    so it is edited to say so. Reporting a removal that did not happen would be
    a worse lie than the message we were trying to take back.
    """
    item = await _sent(session, gw, acme_support, operator)
    much_later = utcnow() + relay.RETRACTION_WINDOW + timedelta(minutes=1)

    deleted, withdrawn = await relay.retract_relayed_reply(
        session, gw, item, ORIGIN, now=much_later,
    )

    assert (deleted, withdrawn) == (0, 1)
    assert gw.deleted == []
    assert relay.RETRACTED_TEXT in [t for ts in gw.edits.values() for t in ts]


async def test_the_window_is_the_one_telegram_documents() -> None:
    assert relay.RETRACTION_WINDOW == timedelta(hours=48)


async def test_retracting_nothing_is_not_an_error(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    assert await relay.retract_relayed_reply(session, gw, item, 999) == (0, 0)


# --------------------------------------------------------------------------
# Both sides of a two-sided request
# --------------------------------------------------------------------------

async def test_one_composition_can_produce_two_copies(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """A reply on a bridged request can go to each side separately, and both
    came from the same typing. Correcting one and leaving the other would be
    worse than correcting neither — the two sides would then be reading
    different things with nobody aware of it."""
    item = await _open(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    for target in (acme_support, pexi_supplier):
        await relay.send_client_reply(
            session, gw, item, Actor.of(operator), "Same message, both sides.",
            to_chat=target, origin_message_id=ORIGIN,
        )

    assert len(await relay.relayed_copies_of(session, ORIGIN)) == 2
    assert await relay.edit_relayed_reply(session, gw, item, ORIGIN, "Corrected.") == 2


async def _code(session, chat, code):
    """Give a counterparty a four-letter code.

    Set explicitly because nothing assigns one automatically, in tests or in
    production — `/npsetcode` does it by hand. Without this both sides fall
    back to a bare "#1042" and are identical, which would make the assertion
    below compare a string to itself and pass no matter what the code did.

    Worth knowing when reading the older two-sided tests: they assert
    `"ACME" not in seen` against fixtures that have no codes at all, so they
    are true whatever happens. The leak they were written for was real and was
    found by looking at Telegram, not by them.
    """
    await session.refresh(chat, ["client"])
    chat.client.code = code
    await session.flush()


async def test_each_side_keeps_its_own_reference_when_corrected(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """The leak guard has to survive an edit too.

    A correction rebuilds the header, and rebuilding it from the wrong
    reference would hand the supplier the client's code — the exact fault found
    on 16 September, reintroduced through a door that did not exist then.
    """
    await _code(session, acme_support, "ACME")
    await _code(session, pexi_supplier, "SPEX")

    item = await _open(session, gw, acme_support)
    item.bridged_chat_id = pexi_supplier.id
    await session.flush()

    for target in (acme_support, pexi_supplier):
        await relay.send_client_reply(
            session, gw, item, Actor.of(operator), "Both sides.",
            to_chat=target, origin_message_id=ORIGIN,
        )
    await relay.edit_relayed_reply(session, gw, item, ORIGIN, "Corrected.")

    copies = {c.telegram_chat_id: c.text for c in
              await relay.relayed_copies_of(session, ORIGIN)}

    assert "SPEX" in copies[pexi_supplier.telegram_chat_id]
    assert "ACME" not in copies[pexi_supplier.telegram_chat_id]
    assert "ACME" in copies[acme_support.telegram_chat_id]
