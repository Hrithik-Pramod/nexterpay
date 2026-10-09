"""When a counterparty taps one of our buttons, the desk hears about it.

NexterPay, 29 September, after accepting a rate from the client group:

    I didn't receive any sort of confirmation that client accepted the rate
    or so.

Everything this platform sent *outward* announced itself into the Operations
topic — rate sent, order sent, settlement passed, rejection notified. Nothing
that came *back* did. The events were recorded, so the history was complete
and `/npfx` would have answered; but a desk waiting on a client is not going
to poll a command, and the point of the topic is that the conversation appears
in it.

Since 7 October the first half of that is gone: the desk is no longer told
about its own sends, because Jason asked for the bot to stop holding a
dialogue with Slim about things Slim had just done. What this file covers is
the half that was added then and matters more — the other side moving. The
fault it was written for was the platform being loud about us and silent
about them; the fix was never to be loud about both.

Four buttons a counterparty can tap, and all four were silent internally: the
rate Yes, the rate No, the order confirmation, and the receipt.

The same report carried two more faults in the same screenshot, both covered
below: the client was thanked twice for the same Yes, and was told "we will
send the order through shortly" when nobody had said how much they wanted to
trade.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Client
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import EventType, FxOrderStatus
from app.domain.work_items import Actor
from app.services.gateway import FakeGateway

OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _quoted_deal(session, acme_support, operator, *, thread_id=7001):
    """A deal quoted to the client and waiting on their answer."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="EUR to XOF",
        original_message="Please provide a rate for XOF.",
        raised_by_name="Gavs D",
    )
    await wi.attach_topic(session, item, thread_id)
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()

    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    await fx.quote_client(
        session, order, rate=Decimal("610"),
        actor=Actor.of(operator), currency_code="XOF",
    )
    return item, order


# --------------------------------------------------------------------------
# Accepting twice
# --------------------------------------------------------------------------

async def test_a_rate_can_only_be_accepted_once(
    session, acme_support, support_ops, operator
):
    """The fault in the screenshot: two "we will send the order through
    shortly", a minute apart.

    Every other step on a deal is protected by `_require_state` - the first
    action moves the deal and the second finds the wrong state. Accepting a
    rate deliberately does not move it, so that guard can never fire here and
    the client could tap Yes as often as they liked.
    """
    _, order = await _quoted_deal(session, acme_support, operator)
    client = Actor(name="Gavs D", telegram_user_id=900)

    await fx.client_accepts_rate(session, order, actor=client)

    with pytest.raises(fx.FxError):
        await fx.client_accepts_rate(session, order, actor=client)


async def test_accepting_still_does_not_move_the_deal(
    session, acme_support, support_ops, operator
):
    """The guard must not be bought by changing the status.

    Saying yes to a price is not agreeing an order - there are no amounts yet.
    Moving the deal here would leave it reading Awaiting client confirmation
    with nothing for the client to confirm, which is why it was written this
    way; the second answer had to be caught some other way.
    """
    _, order = await _quoted_deal(session, acme_support, operator)
    await fx.client_accepts_rate(
        session, order, actor=Actor(name="Gavs D", telegram_user_id=900)
    )
    assert order.status is FxOrderStatus.RATE_QUOTED


async def test_the_guard_is_the_event_not_the_status(
    session, acme_support, support_ops, operator
):
    _, order = await _quoted_deal(session, acme_support, operator)
    assert not await fx.has_event(session, order, EventType.FX_RATE_ACCEPTED)

    await fx.client_accepts_rate(
        session, order, actor=Actor(name="Gavs D", telegram_user_id=900)
    )
    assert await fx.has_event(session, order, EventType.FX_RATE_ACCEPTED)


async def test_two_deals_on_one_request_are_told_apart(
    session, acme_support, support_ops, operator
):
    """Events live against the client's work item, and one request can carry
    more than one deal. "Has this been accepted" has to mean this deal rather
    than any deal on the request, or accepting the first would silently block
    the second."""
    item, first = await _quoted_deal(session, acme_support, operator)
    client_row = await session.get(Client, item.client_id)
    second = await fx.open_order(
        session, client=client_row, client_work_item=item, actor=Actor.of(operator)
    )
    await fx.quote_client(
        session, second, rate=Decimal("615"),
        actor=Actor.of(operator), currency_code="XOF",
    )

    await fx.client_accepts_rate(
        session, first, actor=Actor(name="Gavs D", telegram_user_id=900)
    )

    assert await fx.has_event(session, first, EventType.FX_RATE_ACCEPTED)
    assert not await fx.has_event(session, second, EventType.FX_RATE_ACCEPTED)


# --------------------------------------------------------------------------
# Telling the desk
# --------------------------------------------------------------------------

async def test_the_desk_is_told_the_client_accepted(
    session, acme_support, support_ops, operator, gw
):
    from app.domain.enums import FxSide
    from app.services import fx_relay

    _, order = await _quoted_deal(session, acme_support, operator)
    await fx_relay.announce_counterparty_reply(
        session, gw, order, FxSide.CLIENT, "Gavs D accepted the rate of 610.00 XOF.",
    )

    said = gw.all_text_to(OPS_CHAT)
    assert "accepted the rate" in said
    assert "610.00" in said


async def test_the_announcement_lands_in_the_deals_own_topic(
    session, acme_support, support_ops, operator, gw
):
    """Not the group at large. A desk running four deals needs it against the
    one it belongs to."""
    from app.domain.enums import FxSide
    from app.services import fx_relay

    item, order = await _quoted_deal(session, acme_support, operator, thread_id=7009)
    await fx_relay.announce_counterparty_reply(
        session, gw, order, FxSide.CLIENT, "Gavs D accepted the rate.",
    )

    threads = [
        call.payload.get("thread_id")
        for call in gw.calls
        if call.method == "send_message" and call.chat_id == OPS_CHAT
    ]
    assert item.topic_id in threads


async def test_it_never_reaches_the_counterparty(
    session, acme_support, support_ops, operator, gw
):
    """Internal commentary, like every other line `_announce` writes. It names
    both sides and says what to do next, neither of which is the client's."""
    from app.domain.enums import FxSide
    from app.services import fx_relay

    _, order = await _quoted_deal(session, acme_support, operator)
    await fx_relay.announce_counterparty_reply(
        session, gw, order, FxSide.CLIENT,
        "Gavs D accepted the rate. Build the order once you have the amount.",
    )

    assert "Build the order" not in gw.all_text_to(acme_support.telegram_chat_id)


# --------------------------------------------------------------------------
# What the client is told
# --------------------------------------------------------------------------

def test_the_client_is_not_promised_an_order_we_cannot_send() -> None:
    """NexterPay: "It's stating up here we will send your order shortly, but we
    do not have a value they wish to trade."

    They were right, and it was a promise the platform could not keep.
    Accepting a rate agrees a price; there is no order until somebody says how
    much. Checked on the source because the wording is the fault.
    """
    import pathlib

    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")
    accept = source[source.index("async def client_accepts_the_rate"):]
    accept = accept[: accept.index("@router.callback_query(F.data.startswith(\"fx:rateno:\"))")]

    # Comments stripped first. The note explaining this change quotes the old
    # promise, and the first version of this test failed on its own
    # explanation - which is worth leaving written down, because a source
    # check that reads comments is a source check that will do it again.
    code = "\n".join(
        line for line in accept.splitlines() if not line.strip().startswith("#")
    )

    assert "we will send the order through shortly" not in code
    assert "How much would you like to trade" in code


def test_every_counterparty_button_clears_itself() -> None:
    """The other half of not being answered twice. The domain refuses a second
    answer; this stops one being offered, which is what the tester did - the
    buttons were still there, so tapping again was the obvious thing to do."""
    import pathlib

    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")
    for handler, nxt in (
        ("async def client_accepts_the_rate", 'F.data.startswith("fx:rateno:")'),
        ("async def client_declines_the_rate", "# ---"),
        ("async def counterparty_confirms", 'F.data.startswith("fx:receipt:")'),
        ("async def client_confirms_receipt", 'F.data.startswith("fx:cancel:")'),
    ):
        body = source[source.index(handler):]
        body = body[: body.index(nxt)]
        assert "_clear_buttons(query)" in body, f"{handler} leaves its buttons live"
