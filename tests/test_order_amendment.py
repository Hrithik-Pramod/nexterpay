"""Two ways a deal's figures change, with opposite handling.

NexterPay, through Jason, over two days. On 3 October:

    No it should match, or if the supplier does not have enough, the order
    amount may change.

That was read here as one thing - an amendment - and built as one function.
Two questions were put back on 4 October to check the assumptions underneath
it, and both answers were the opposite of what had been built:

    Does the rate stay the same, only the amount moves?
    — No it should not, but we have had occasions where after a deal is
      agreed, stock issues cause rates to change, they wont settle on that
      rate, they will notify us of change before we agree it with client.

    Does the client have to confirm the new amount?
    — No we make the decision on the short.

So they are two events, not one, and they point in opposite directions.

**A short amount is NexterPay's decision.** The deal does not move and the
client is told rather than asked. The platform's job is to record a call they
have already made, not to invent an approval step they do not want.

**A changed rate is the client's decision.** A price is the one thing the
client agreed to, so a new price is a new offer: the deal goes back to being
quoted and they accept it or they do not.

The first version of this module had both backwards, which is worth leaving
written down. The figures do not tell you who decides - that is a fact about
the agreement NexterPay have with their clients, and the only way to know it
was to ask.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Client, Event
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import EventType, FxOrderStatus
from app.domain.history import render_event
from app.domain.work_items import Actor


async def _agreed_order(session, acme_support, operator, *, status=None):
    """A deal with figures both sides have agreed."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="EUR to XOF",
        original_message="Please provide a rate.",
        raised_by_name="Gavs D",
    )
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    order.client_rate = Decimal("612")
    order.client_pays = Decimal("250000")
    order.client_pays_currency = "EUR"
    order.client_receives = Decimal("153000000")
    order.client_receives_currency = "XOF"
    order.supplier_rate = Decimal("605")
    order.supplier_receives = Decimal("151250000")
    order.currency_code = "XOF"
    order.country_code = "CI"
    order.status = status or FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()
    return order


async def _amendment_rows(session, order):
    result = await session.execute(
        Event.__table__.select().where(
            Event.work_item_id == order.client_work_item_id
        )
    )
    return [r for r in result.fetchall()
            if r.event_type is EventType.FX_ORDER_AMENDED]


# --------------------------------------------------------------------------
# A short amount: NexterPay decide
# --------------------------------------------------------------------------

async def test_the_amount_comes_down_when_the_supplier_is_short(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)

    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        supplier_receives=Decimal("121000000"),
        reason="Supplier could only fund 200k",
        actor=Actor.of(operator),
    )

    assert order.client_pays == Decimal("200000")
    assert order.client_receives == Decimal("122400000")
    assert order.supplier_receives == Decimal("121000000")


async def test_the_deal_does_not_move_and_the_client_is_not_asked(
    session, acme_support, support_ops, operator
):
    """Jason, 4 October: "No we make the decision on the short."

    Built the other way round first. The deal went back to the client to
    confirm, on the reasoning that their receipt had changed - which is sound
    reasoning and not what NexterPay do. Who decides is a fact about their
    agreement with their clients, and it is not derivable from the figures.
    """
    order = await _agreed_order(session, acme_support, operator)

    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier short",
        actor=Actor.of(operator),
    )

    assert order.status is FxOrderStatus.AWAITING_SETTLEMENT


async def test_a_short_amount_leaves_the_rate_alone(
    session, acme_support, support_ops, operator
):
    """A rate that moves is a different event with the opposite handling."""
    order = await _agreed_order(session, acme_support, operator)

    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier short",
        actor=Actor.of(operator),
    )

    assert order.client_rate == Decimal("612")
    assert order.supplier_rate == Decimal("605")


async def test_an_amount_change_needs_a_reason(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    with pytest.raises(fx.FxError):
        await fx.amend_amount(
            session, order,
            client_pays=Decimal("200000"),
            client_receives=Decimal("122400000"),
            reason="   ", actor=Actor.of(operator),
        )


async def test_an_amended_order_is_still_for_something(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    with pytest.raises(fx.FxError):
        await fx.amend_amount(
            session, order,
            client_pays=Decimal("0"), client_receives=Decimal("0"),
            reason="Supplier had nothing", actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# A changed rate: the client decides
# --------------------------------------------------------------------------

async def test_a_rate_change_goes_back_to_the_client(
    session, acme_support, support_ops, operator
):
    """Jason, 4 October: "stock issues cause rates to change, they wont settle
    on that rate, they will notify us of change before we agree it with
    client."

    A price is the one thing the client agreed to, so a new price is a new
    offer. The desk does not get to decide this one on their behalf.
    """
    order = await _agreed_order(session, acme_support, operator)

    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"),
        client_rate=Decimal("604"),
        reason="Supplier stock issue, rate moved",
        actor=Actor.of(operator),
    )

    assert order.status is FxOrderStatus.RATE_QUOTED
    assert order.client_rate == Decimal("604")
    assert order.supplier_rate == Decimal("598")


async def test_repricing_clears_both_agreements(
    session, acme_support, support_ops, operator
):
    """Neither side agreed to this price. The supplier's acceptance was of an
    order built on the old one."""
    order = await _agreed_order(session, acme_support, operator)
    order.client_confirmed_at = fx.utcnow()
    order.supplier_confirmed_at = fx.utcnow()
    await session.flush()

    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"), client_rate=Decimal("604"),
        reason="Stock issue", actor=Actor.of(operator),
    )

    assert order.client_confirmed_at is None
    assert order.supplier_confirmed_at is None


async def test_repricing_will_not_sell_at_a_loss(
    session, acme_support, support_ops, operator
):
    """The margin guard applies to a new price exactly as it does to a first
    one. A rate that moved under pressure is the most likely moment for
    somebody to quote below cost."""
    order = await _agreed_order(session, acme_support, operator)

    with pytest.raises(fx.FxError):
        await fx.reprice(
            session, order,
            supplier_rate=Decimal("610"), client_rate=Decimal("605"),
            reason="Stock issue", actor=Actor.of(operator),
        )


async def test_repricing_needs_a_reason(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    with pytest.raises(fx.FxError):
        await fx.reprice(
            session, order,
            supplier_rate=Decimal("598"), client_rate=Decimal("604"),
            reason="", actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# When either may happen
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", list(fx.AMENDABLE))
async def test_the_states_an_amount_can_change_in(
    session, acme_support, support_ops, operator, status
):
    order = await _agreed_order(session, acme_support, operator, status=status)
    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"), client_receives=Decimal("122400000"),
        reason="Supplier short", actor=Actor.of(operator),
    )
    assert order.status is status, "an amount change never moves the deal"


@pytest.mark.parametrize(
    "status",
    [FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
     FxOrderStatus.AWAITING_SETTLEMENT],
)
async def test_the_states_a_rate_can_change_in(
    session, acme_support, support_ops, operator, status
):
    order = await _agreed_order(session, acme_support, operator, status=status)
    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"), client_rate=Decimal("604"),
        reason="Stock issue", actor=Actor.of(operator),
    )
    assert order.status is FxOrderStatus.RATE_QUOTED


@pytest.mark.parametrize(
    "status",
    [FxOrderStatus.RATE_REQUESTED, FxOrderStatus.AWAITING_RECEIPT,
     FxOrderStatus.CLOSED],
)
async def test_figures_cannot_change_outside_that_window(
    session, acme_support, support_ops, operator, status
):
    """Earlier there is nothing agreed to change. Once the money has moved,
    the figures are a record of what was paid rather than what was agreed,
    and that is not ours to rewrite."""
    order = await _agreed_order(session, acme_support, operator, status=status)

    with pytest.raises(fx.FxError):
        await fx.amend_amount(
            session, order,
            client_pays=Decimal("200000"), client_receives=Decimal("122400000"),
            reason="Supplier short", actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# The record afterwards
# --------------------------------------------------------------------------

async def test_the_two_kinds_are_told_apart_in_the_record(
    session, acme_support, support_ops, operator
):
    """They share an event type and are not the same event. Somebody reading
    the history in three months needs to know whether NexterPay made the call
    or the client did."""
    order = await _agreed_order(session, acme_support, operator)
    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"), client_receives=Decimal("122400000"),
        reason="Supplier short", actor=Actor.of(operator),
    )
    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"), client_rate=Decimal("604"),
        reason="Stock issue", actor=Actor.of(operator),
    )

    kinds = [row.payload.get("kind") for row in await _amendment_rows(session, order)]
    assert kinds == ["amount", "rate"]


async def test_an_amount_event_carries_what_it_used_to_be(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"), client_receives=Decimal("122400000"),
        reason="Supplier could only fund 200k", actor=Actor.of(operator),
    )

    row = (await _amendment_rows(session, order))[-1]
    assert row.payload["was_client_pays"] == "250000"
    assert row.payload["client_pays"] == "200000"
    assert row.payload["reason"] == "Supplier could only fund 200k"


async def test_a_rate_event_carries_the_old_price(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"), client_rate=Decimal("604"),
        reason="Stock issue", actor=Actor.of(operator),
    )

    row = (await _amendment_rows(session, order))[-1]
    assert row.payload["was_client_rate"] == "612"
    assert row.payload["client_rate"] == "604"


async def test_the_history_lines_read_differently(
    session, acme_support, support_ops, operator
):
    order = await _agreed_order(session, acme_support, operator)
    await fx.amend_amount(
        session, order,
        client_pays=Decimal("200000"), client_receives=Decimal("122400000"),
        reason="Supplier short", actor=Actor.of(operator),
    )
    amount_line = render_event(
        await session.get(Event, (await _amendment_rows(session, order))[-1].id),
        verbose=True,
    )

    await fx.reprice(
        session, order,
        supplier_rate=Decimal("598"), client_rate=Decimal("604"),
        reason="Stock issue", actor=Actor.of(operator),
    )
    rate_line = render_event(
        await session.get(Event, (await _amendment_rows(session, order))[-1].id),
        verbose=True,
    )

    assert "250000" in amount_line and "200000" in amount_line
    assert "confirm" not in amount_line.lower(), (
        "an amount change is NexterPay's decision - the line must not imply "
        "the client was asked"
    )
    assert "612" in rate_line and "604" in rate_line
    assert "agree" in rate_line.lower()
