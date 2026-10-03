"""One payment, many orders, many currencies.

Built from a week of NexterPay's real supplier chat, forwarded by Jason on
3 October. Two settlements from it are used as the test data, because figures
somebody actually sent are worth more than figures invented to pass:

    XAF: 3000000/606  = 4,950.495
    XOF: 20100000/585 = 34,358.974
    ≡ 39 309,469 USDT ✅
    51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474

and, a week later:

    XOF: 50250000/583 = 86 192,11
    XOF: 45000000/583 = 77 186,964
    86192 + 77186 = 163 378 USDT
    ba982263a27748cb69727fec6d974330a0fc5856190bfafa417d69e98ba34a4a

The second one is wrong, and that is why it is here. Both lines were rounded
down to whole USDT before being added, so the payment is 163,378 against
orders totalling 163,379.07 - about one USDT short. Jason confirmed it was a
slip. It cost nothing this time; the same slip on a larger number, or in the
other direction, does not.

The thing this module deliberately cannot do is as important as what it can.
There is no balance, no remainder, no running account against a supplier.
NexterPay were asked what happens when a payment does not match and said it
should match, or the order amount changes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Client, SettlementAllocation
from app.domain import corridors, fx, settlement
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor


async def _settleable(session, acme_support, operator, *, subject="EUR to XOF"):
    """An order sitting where a settlement can act on it."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject=subject,
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
    order.status = FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()
    return order


HASH_A = "51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474"
HASH_B = "ba982263a27748cb69727fec6d974330a0fc5856190bfafa417d69e98ba34a4a"


# --------------------------------------------------------------------------
# Their first settlement, exactly
# --------------------------------------------------------------------------

async def test_one_payment_covers_two_countries_and_two_currencies(
    session, acme_support, support_ops, operator
):
    """The case that prompted the whole model.

    XAF and XOF on one hash. The payment has no currency of its own - it is
    USDT - and the two currencies live on the allocations underneath.
    """
    cm = await _settleable(session, acme_support, operator, subject="XAF deal")
    sn = await _settleable(session, acme_support, operator, subject="XOF deal")

    lines = [
        settlement.Line(cm, "CM", Decimal("3000000"), Decimal("606")),
        settlement.Line(sn, "SN", Decimal("20100000"), Decimal("585")),
    ]
    record = await settlement.record(
        session, lines=lines, tx_hash=HASH_A,
        amount_usdt=Decimal("39309.469"), actor=Actor.of(operator),
    )

    allocations = await settlement.allocations_of(session, record)
    assert [a.country_code for a in allocations] == ["CM", "SN"]
    assert {a.fx_order_id for a in allocations} == {cm.id, sn.id}


async def test_the_lines_convert_the_way_they_wrote_them(
    session, acme_support, support_ops, operator
):
    """3000000/606 = 4,950.495 and 20100000/585 = 34,358.974."""
    cm = await _settleable(session, acme_support, operator, subject="a")
    sn = await _settleable(session, acme_support, operator, subject="b")
    lines = [
        settlement.Line(cm, "CM", Decimal("3000000"), Decimal("606")),
        settlement.Line(sn, "SN", Decimal("20100000"), Decimal("585")),
    ]

    assert round(lines[0].usdt, 3) == Decimal("4950.495")
    assert round(lines[1].usdt, 3) == Decimal("34358.974")


async def test_the_total_is_the_one_they_sent(
    session, acme_support, support_ops, operator
):
    """39,309.469. Their sum was right on this one."""
    cm = await _settleable(session, acme_support, operator, subject="a")
    sn = await _settleable(session, acme_support, operator, subject="b")
    lines = [
        settlement.Line(cm, "CM", Decimal("3000000"), Decimal("606")),
        settlement.Line(sn, "SN", Decimal("20100000"), Decimal("585")),
    ]

    assert round(settlement.expected_total(lines), 3) == Decimal("39309.469")
    assert not settlement.is_material(
        settlement.discrepancy(lines, Decimal("39309.469"))
    )


async def test_the_currency_comes_from_the_country(
    session, acme_support, support_ops, operator
):
    """CM is XAF, SN is XOF - and neither could have been worked out from the
    currency, which is the whole reason the country is what gets stored."""
    cm = await _settleable(session, acme_support, operator, subject="a")
    sn = await _settleable(session, acme_support, operator, subject="b")

    assert settlement.Line(cm, "CM", Decimal(1), Decimal(1)).currency_code == "XAF"
    assert settlement.Line(sn, "SN", Decimal(1), Decimal(1)).currency_code == "XOF"


# --------------------------------------------------------------------------
# Their second settlement, which was a pound short
# --------------------------------------------------------------------------

async def test_the_seventh_of_september_slip_is_caught(
    session, acme_support, support_ops, operator
):
    """86192 + 77186 = 163 378, against orders totalling 163,379.07.

    They rounded both lines down to whole USDT before adding. Jason confirmed
    it was a slip rather than a convention. This is the test that would have
    told them on the day.
    """
    first = await _settleable(session, acme_support, operator, subject="a")
    second = await _settleable(session, acme_support, operator, subject="b")
    lines = [
        settlement.Line(first, "CI", Decimal("50250000"), Decimal("583")),
        settlement.Line(second, "CI", Decimal("45000000"), Decimal("583")),
    ]

    expected = settlement.expected_total(lines)
    assert round(expected, 2) == Decimal("163379.07")

    difference = settlement.discrepancy(lines, Decimal("163378"))
    assert difference < 0, "they paid less than the orders came to"
    assert round(difference, 2) == Decimal("-1.07")


async def test_a_rounding_difference_is_not_worth_shouting_about(
    session, acme_support, support_ops, operator
):
    """The eighth decimal place of a conversion nobody rounded on purpose.

    A tolerance of zero would make the platform complain about every
    settlement, which is the fastest way to make somebody stop reading what it
    says.
    """
    order = await _settleable(session, acme_support, operator)
    lines = [settlement.Line(order, "CI", Decimal("50250000"), Decimal("583"))]
    nearly = settlement.expected_total(lines) - Decimal("0.004")

    assert not settlement.is_material(settlement.discrepancy(lines, nearly))


async def test_the_difference_keeps_its_sign(
    session, acme_support, support_ops, operator
):
    """Short and over are different problems. Short means a client is waiting
    on money that is not coming; over means NexterPay have given margin away.
    A magnitude would hide which."""
    order = await _settleable(session, acme_support, operator)
    lines = [settlement.Line(order, "CI", Decimal("5830"), Decimal("583"))]  # 10 USDT

    assert settlement.discrepancy(lines, Decimal("8")) == Decimal("-2")
    assert settlement.discrepancy(lines, Decimal("13")) == Decimal("3")


async def test_nothing_claimed_to_have_moved_is_not_a_discrepancy(
    session, acme_support, support_ops, operator
):
    """A settlement recorded before the payment figure is known is not a
    settlement that is wrong."""
    order = await _settleable(session, acme_support, operator)
    lines = [settlement.Line(order, "CI", Decimal("5830"), Decimal("583"))]

    assert settlement.discrepancy(lines, None) is None
    assert not settlement.is_material(None)


# --------------------------------------------------------------------------
# What a settlement does to the deals on it
# --------------------------------------------------------------------------

async def test_every_order_moves_to_awaiting_receipt(
    session, acme_support, support_ops, operator
):
    """Settling is not closing. The deal ends when the client confirms they
    have the funds, which is where /nphash left a single order before this
    existed."""
    first = await _settleable(session, acme_support, operator, subject="a")
    second = await _settleable(session, acme_support, operator, subject="b")
    lines = [
        settlement.Line(first, "CI", Decimal("5830"), Decimal("583")),
        settlement.Line(second, "SN", Decimal("5830"), Decimal("583")),
    ]

    await settlement.record(
        session, lines=lines, tx_hash=HASH_A,
        amount_usdt=Decimal("20"), actor=Actor.of(operator),
    )

    assert first.status is FxOrderStatus.AWAITING_RECEIPT
    assert second.status is FxOrderStatus.AWAITING_RECEIPT


async def test_the_hash_reaches_every_order_on_the_payment(
    session, acme_support, support_ops, operator
):
    """One payment, so one hash, and a client asking about their own deal
    should be given the proof that covers it."""
    first = await _settleable(session, acme_support, operator, subject="a")
    second = await _settleable(session, acme_support, operator, subject="b")
    lines = [
        settlement.Line(first, "CI", Decimal("5830"), Decimal("583")),
        settlement.Line(second, "SN", Decimal("5830"), Decimal("583")),
    ]

    await settlement.record(
        session, lines=lines, tx_hash=HASH_A,
        amount_usdt=Decimal("20"), actor=Actor.of(operator),
    )

    assert first.tx_hash == HASH_A
    assert second.tx_hash == HASH_A


async def test_an_order_settles_only_once(
    session, acme_support, support_ops, operator
):
    """There is no partial settlement to model. NexterPay: it should match, or
    the order amount changes."""
    order = await _settleable(session, acme_support, operator)
    lines = [settlement.Line(order, "CI", Decimal("5830"), Decimal("583"))]
    await settlement.record(
        session, lines=lines, tx_hash=HASH_A,
        amount_usdt=Decimal("10"), actor=Actor.of(operator),
    )

    order.status = FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()

    with pytest.raises(settlement.SettlementError):
        await settlement.record(
            session,
            lines=[settlement.Line(order, "CI", Decimal("5830"), Decimal("583"))],
            tx_hash=HASH_B, amount_usdt=Decimal("10"), actor=Actor.of(operator),
        )


async def test_an_order_not_awaiting_settlement_is_refused(
    session, acme_support, support_ops, operator
):
    order = await _settleable(session, acme_support, operator)
    order.status = FxOrderStatus.RATE_QUOTED
    await session.flush()

    with pytest.raises(settlement.SettlementError):
        await settlement.record(
            session,
            lines=[settlement.Line(order, "CI", Decimal("5830"), Decimal("583"))],
            tx_hash=HASH_A, amount_usdt=Decimal("10"), actor=Actor.of(operator),
        )


async def test_one_bad_order_records_nothing_at_all(
    session, acme_support, support_ops, operator
):
    """A settlement half-applied across four orders is worse than one not
    applied at all, because the half that went through is invisible. Every
    order is checked before any is touched."""
    good = await _settleable(session, acme_support, operator, subject="a")
    bad = await _settleable(session, acme_support, operator, subject="b")
    bad.status = FxOrderStatus.RATE_QUOTED
    await session.flush()

    with pytest.raises(settlement.SettlementError):
        await settlement.record(
            session,
            lines=[
                settlement.Line(good, "CI", Decimal("5830"), Decimal("583")),
                settlement.Line(bad, "SN", Decimal("5830"), Decimal("583")),
            ],
            tx_hash=HASH_A, amount_usdt=Decimal("20"), actor=Actor.of(operator),
        )

    assert good.status is FxOrderStatus.AWAITING_SETTLEMENT
    assert not await settlement.already_settled(session, good)


async def test_an_empty_settlement_is_refused(
    session, acme_support, support_ops, operator
):
    with pytest.raises(settlement.SettlementError):
        await settlement.record(
            session, lines=[], tx_hash=HASH_A,
            amount_usdt=Decimal("10"), actor=Actor.of(operator),
        )


async def test_an_unknown_country_is_refused(
    session, acme_support, support_ops, operator
):
    """A country with no currency produces a line priced against nothing."""
    order = await _settleable(session, acme_support, operator)

    with pytest.raises(corridors.UnknownCountry):
        await settlement.record(
            session,
            lines=[settlement.Line(order, "ZZ", Decimal("5830"), Decimal("583"))],
            tx_hash=HASH_A, amount_usdt=Decimal("10"), actor=Actor.of(operator),
        )


# --------------------------------------------------------------------------
# The record afterwards
# --------------------------------------------------------------------------

async def test_the_settlement_can_be_found_from_any_order_on_it(
    session, acme_support, support_ops, operator
):
    first = await _settleable(session, acme_support, operator, subject="a")
    second = await _settleable(session, acme_support, operator, subject="b")
    record = await settlement.record(
        session,
        lines=[
            settlement.Line(first, "CI", Decimal("5830"), Decimal("583")),
            settlement.Line(second, "SN", Decimal("5830"), Decimal("583")),
        ],
        tx_hash=HASH_A, amount_usdt=Decimal("20"), actor=Actor.of(operator),
    )

    assert (await settlement.settlement_for(session, first)).id == record.id
    assert (await settlement.settlement_for(session, second)).id == record.id


async def test_the_rate_is_kept_on_the_allocation(
    session, acme_support, support_ops, operator
):
    """Copied rather than read back off the order.

    A settlement records what was paid and at what price. Re-reading a rate
    that has since been requoted would quietly rewrite history, and this is
    the row a dispute comes back to.
    """
    order = await _settleable(session, acme_support, operator)
    await settlement.record(
        session,
        lines=[settlement.Line(order, "CI", Decimal("50250000"), Decimal("583"))],
        tx_hash=HASH_A, amount_usdt=Decimal("86192.11"), actor=Actor.of(operator),
    )

    order.client_rate = Decimal("999")
    await session.flush()

    allocation = (await session.execute(
        SettlementAllocation.__table__.select()
    )).first()
    assert allocation.rate == Decimal("583")


async def test_settlements_are_numbered_in_their_own_sequence(
    session, acme_support, support_ops, operator
):
    first = await _settleable(session, acme_support, operator, subject="a")
    second = await _settleable(session, acme_support, operator, subject="b")

    one = await settlement.record(
        session, lines=[settlement.Line(first, "CI", Decimal("5830"), Decimal("583"))],
        tx_hash=HASH_A, amount_usdt=Decimal("10"), actor=Actor.of(operator),
    )
    two = await settlement.record(
        session, lines=[settlement.Line(second, "SN", Decimal("5830"), Decimal("583"))],
        tx_hash=HASH_B, amount_usdt=Decimal("10"), actor=Actor.of(operator),
    )

    assert one.display_reference == "SET-1000"
    assert two.display_reference == "SET-1001"


async def test_there_is_no_balance_anywhere(
    session, acme_support, support_ops, operator
):
    """The biggest thing this phase is not.

    NexterPay were asked what happens to a difference and said the settlement
    should match, or the order amount changes. No remainder is carried against
    a counterparty. If a balance column ever appears on a settlement, somebody
    has started building a ledger and should say so out loud first.
    """
    from app.db.models import Settlement

    columns = set(Settlement.__table__.columns.keys())
    assert not {c for c in columns if "balance" in c or "remainder" in c}
