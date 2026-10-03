"""An order's figures can change after both sides have agreed them.

NexterPay, through Jason on 3 October. Asked what happens when a settlement
does not cover the orders it is meant to:

    No it should match, or if the supplier does not have enough, the order
    amount may change.

That sentence asked for something the platform could not do, and it was not
obvious that it had. An order was fixed the moment the client confirmed it -
that is the entire purpose of the confirm step - so "the order amount may
change" meant either cancelling the deal and losing its history, or a path
back into figures that were supposed to be settled.

Three things about the path are deliberate, and each is a test below.

**The rate does not move.** A supplier being short of liquidity is not a
reason for the client to get a different price. Only amounts change.

**The client confirms again.** Their agreement was to the figure that has
just changed, so it does not carry. Slower, and the only honest version.

**A reason is required.** "The supplier was short" and "we typed it wrong"
are different answers to the question somebody asks in three months.
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


async def _confirmed_order(session, acme_support, operator, *, status=None):
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


# --------------------------------------------------------------------------
# The case Jason described
# --------------------------------------------------------------------------

async def test_the_supplier_is_short_and_the_amount_comes_down(
    session, acme_support, support_ops, operator
):
    order = await _confirmed_order(session, acme_support, operator)

    await fx.amend_order(
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


async def test_the_rate_does_not_move(
    session, acme_support, support_ops, operator
):
    """A supplier being short of liquidity is not a reason for the client to
    get a different price.

    If NexterPay ever want the rate to move too, that is a different function
    and a different conversation - it would mean repricing a deal the client
    has already said yes to.
    """
    order = await _confirmed_order(session, acme_support, operator)

    await fx.amend_order(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier short",
        actor=Actor.of(operator),
    )

    assert order.client_rate == Decimal("612")
    assert order.supplier_rate == Decimal("605")


async def test_the_client_has_to_confirm_again(
    session, acme_support, support_ops, operator
):
    """Their agreement was to the figure that just changed."""
    order = await _confirmed_order(session, acme_support, operator)
    order.client_confirmed_at = fx.utcnow()
    order.supplier_confirmed_at = fx.utcnow()
    await session.flush()

    await fx.amend_order(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier short",
        actor=Actor.of(operator),
    )

    assert order.status is FxOrderStatus.AWAITING_CLIENT_CONFIRMATION
    assert order.client_confirmed_at is None
    assert order.supplier_confirmed_at is None


async def test_a_reason_is_required(
    session, acme_support, support_ops, operator
):
    order = await _confirmed_order(session, acme_support, operator)

    with pytest.raises(fx.FxError):
        await fx.amend_order(
            session, order,
            client_pays=Decimal("200000"),
            client_receives=Decimal("122400000"),
            reason="   ",
            actor=Actor.of(operator),
        )


async def test_an_amended_order_is_still_for_something(
    session, acme_support, support_ops, operator
):
    order = await _confirmed_order(session, acme_support, operator)

    with pytest.raises(fx.FxError):
        await fx.amend_order(
            session, order,
            client_pays=Decimal("0"),
            client_receives=Decimal("0"),
            reason="Supplier had nothing",
            actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# When it may happen
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", list(fx.AMENDABLE))
async def test_the_states_an_order_can_be_amended_from(
    session, acme_support, support_ops, operator, status
):
    order = await _confirmed_order(session, acme_support, operator, status=status)

    await fx.amend_order(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier short",
        actor=Actor.of(operator),
    )
    assert order.status is FxOrderStatus.AWAITING_CLIENT_CONFIRMATION


@pytest.mark.parametrize(
    "status",
    [FxOrderStatus.RATE_REQUESTED, FxOrderStatus.RATE_QUOTED,
     FxOrderStatus.AWAITING_RECEIPT, FxOrderStatus.CLOSED],
)
async def test_the_states_it_cannot(
    session, acme_support, support_ops, operator, status
):
    """Earlier than AWAITING_CLIENT_CONFIRMATION there are no agreed figures
    to amend - they are still being built. Once the money has moved, the
    amount stops being a question of what was agreed and becomes a record of
    what was paid, which is the settlement's business and not one to rewrite.
    """
    order = await _confirmed_order(session, acme_support, operator, status=status)

    with pytest.raises(fx.FxError):
        await fx.amend_order(
            session, order,
            client_pays=Decimal("200000"),
            client_receives=Decimal("122400000"),
            reason="Supplier short",
            actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# What the record says afterwards
# --------------------------------------------------------------------------

async def _amendment_event(session, order):
    result = await session.execute(
        Event.__table__.select().where(
            Event.work_item_id == order.client_work_item_id
        )
    )
    rows = [r for r in result.fetchall()
            if r.event_type is EventType.FX_ORDER_AMENDED]
    return rows[-1] if rows else None


async def test_the_event_carries_the_figure_it_used_to_be(
    session, acme_support, support_ops, operator
):
    """So the audit trail can answer "what was it before" without a second
    set of columns on the order that would have to be kept honest."""
    order = await _confirmed_order(session, acme_support, operator)

    await fx.amend_order(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier could only fund 200k",
        actor=Actor.of(operator),
    )

    row = await _amendment_event(session, order)
    assert row is not None
    assert row.payload["was_client_pays"] == "250000"
    assert row.payload["client_pays"] == "200000"
    assert row.payload["reason"] == "Supplier could only fund 200k"


async def test_the_history_line_reads_as_a_change(
    session, acme_support, support_ops, operator
):
    order = await _confirmed_order(session, acme_support, operator)
    await fx.amend_order(
        session, order,
        client_pays=Decimal("200000"),
        client_receives=Decimal("122400000"),
        reason="Supplier could only fund 200k",
        actor=Actor.of(operator),
    )

    event = await session.get(
        Event, (await _amendment_event(session, order)).id
    )
    line = render_event(event, verbose=True)

    assert "250000" in line and "200000" in line
    assert "confirm again" in line
    assert "Supplier could only fund 200k" in line
