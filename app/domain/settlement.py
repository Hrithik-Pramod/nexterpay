"""Recording a payment against the orders it covers.

NexterPay, through Jason on 3 October, with a week of their real supplier chat
behind it. This is what a settlement looks like in their hands:

    XAF: 3000000/606  = 4,950.495
    XOF: 20100000/585 = 34,358.974
    ≡ 39 309,469 USDT ✅
    51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474

Two orders, two countries, two currencies, one payment, one hash.

**The payment is always USDT.** That is the fact the whole module turns on,
and it is the opposite of what "settlements can combine many currencies"
sounded like when Jason first said it. The currencies belong to the orders
underneath; each is converted at its own rate; the payment is the sum. So one
shape covers all three cases they described, with nothing special about any of
them: one allocation is a single-order payment, several is a lump sum, and
several in different currencies is the third case.

What this module deliberately does not have is a balance. NexterPay were asked
what happens when a settlement does not match the orders it covers, and the
answer was that it should match - or, if the supplier is short, the order
amount changes. There is no remainder carried against a counterparty and no
running account, which is the single biggest thing this phase is not.

What it does have is arithmetic nobody has to trust. On 7 September they sent
`86192 + 77186 = 163 378`, having rounded both lines down before adding, and
paid about one USDT less than the orders came to. Jason confirmed it was a
slip. A slip of one USDT costs nothing; the same slip on a bigger number, or
in the other direction, does. So the expected total is computed here and the
difference against what actually moved is reported rather than absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    FxOrder,
    Settlement,
    SettlementAllocation,
    SettlementReferenceCounter,
)
from app.domain import corridors
from app.domain.enums import EventType, FxOrderStatus
from app.domain.errors import DomainError
from app.domain.fx import check_hash, record_event
from app.domain.work_items import Actor

# A settlement may differ from the orders it covers by this much before the
# platform says so. Set at one whole USDT because that is the size of the slip
# that actually happened, and because a tolerance of zero would make the
# platform complain about the eighth decimal place of a conversion nobody
# rounded on purpose.
TOLERANCE_USDT = Decimal("1")


class SettlementError(DomainError):
    """Something was asked of a settlement that does not hold."""


@dataclass(frozen=True)
class Line:
    """One order's share, as the desk types it.

    Mirrors their own line - country, local amount, rate - and computes the
    USDT rather than taking it, because the conversion is the step where the
    hand-written version went wrong.
    """

    order: FxOrder
    country_code: str
    local_amount: Decimal
    rate: Decimal

    @property
    def usdt(self) -> Decimal:
        return self.local_amount / self.rate

    @property
    def currency_code(self) -> str:
        return corridors.currency_for(self.country_code)


async def _next_reference(session: AsyncSession) -> int:
    counter = await session.get(SettlementReferenceCounter, 1, with_for_update=True)
    if counter is None:
        counter = SettlementReferenceCounter(id=1, next_value=1000)
        session.add(counter)
        await session.flush()
    value = counter.next_value
    counter.next_value = value + 1
    await session.flush()
    return value


async def already_settled(session: AsyncSession, order: FxOrder) -> bool:
    """Is this order already on a settlement?

    An order settles once. Checked here as well as by the unique constraint,
    because an IntegrityError surfacing from the middle of a multi-order
    settlement tells the desk nothing about which order was the problem.
    """
    result = await session.execute(
        select(SettlementAllocation).where(
            SettlementAllocation.fx_order_id == order.id
        )
    )
    return result.scalars().first() is not None


def expected_total(lines: list[Line]) -> Decimal:
    """What the orders come to. The figure the payment is checked against."""
    return sum((line.usdt for line in lines), Decimal(0))


def discrepancy(lines: list[Line], amount_usdt: Decimal | None) -> Decimal | None:
    """Paid minus expected, or None when nothing was claimed to have moved.

    Signed on purpose. Short and over are different problems - short means a
    client is waiting on money that is not coming, over means NexterPay have
    given away margin - and a magnitude would hide which.
    """
    if amount_usdt is None:
        return None
    return amount_usdt - expected_total(lines)


def is_material(difference: Decimal | None) -> bool:
    return difference is not None and abs(difference) > TOLERANCE_USDT


async def record(
    session: AsyncSession,
    *,
    lines: list[Line],
    tx_hash: str | None,
    amount_usdt: Decimal | None,
    actor: Actor,
    nexterpay_account: str | None = None,
) -> Settlement:
    """Record one payment against the orders it covers.

    Every order moves to AWAITING_RECEIPT, which is where `/nphash` left a
    single order before this existed - settling is not closing, and the deal
    ends when the client confirms they have the funds.

    Refused rather than partially applied if any order is wrong. A settlement
    half-recorded across four orders is worse than one not recorded at all,
    because the half that went through is invisible.
    """
    if not lines:
        raise SettlementError("A settlement needs at least one order on it.")

    for line in lines:
        order = line.order
        if order.status is not FxOrderStatus.AWAITING_SETTLEMENT:
            raise SettlementError(
                f"{order.display_reference} is {order.status.label}, not "
                f"awaiting settlement, so it cannot go on this payment."
            )
        if await already_settled(session, order):
            raise SettlementError(
                f"{order.display_reference} is already on a settlement."
            )
        if line.rate <= 0:
            raise SettlementError(
                f"{order.display_reference} has a rate of {line.rate}, which "
                f"cannot be divided by."
            )
        corridors.parse_country_code(line.country_code)

    cleaned_hash = check_hash(tx_hash) if tx_hash else None

    settlement = Settlement(
        reference=await _next_reference(session),
        tx_hash=cleaned_hash,
        amount_usdt=amount_usdt,
        nexterpay_account=nexterpay_account,
        recorded_by_staff_id=actor.staff.id if actor.staff else None,
        recorded_by_name=actor.name,
        paid_at=utcnow(),
    )
    session.add(settlement)
    await session.flush()

    for line in lines:
        session.add(
            SettlementAllocation(
                settlement_id=settlement.id,
                fx_order_id=line.order.id,
                country_code=corridors.parse_country_code(line.country_code),
                local_amount=line.local_amount,
                rate=line.rate,
                usdt_amount=line.usdt,
            )
        )
        line.order.tx_hash = cleaned_hash
        line.order.settled_at = settlement.paid_at
        line.order.status = FxOrderStatus.AWAITING_RECEIPT
        await record_event(
            session,
            line.order,
            EventType.FX_HASH_RECORDED,
            actor,
            tx_hash=cleaned_hash,
            settlement=settlement.display_reference,
            country=line.country_code,
            local_amount=line.local_amount,
            rate=line.rate,
            usdt=line.usdt,
        )

    await session.flush()
    return settlement


async def allocations_of(
    session: AsyncSession, settlement: Settlement
) -> list[SettlementAllocation]:
    result = await session.execute(
        select(SettlementAllocation)
        .where(SettlementAllocation.settlement_id == settlement.id)
        .order_by(SettlementAllocation.id)
    )
    return list(result.scalars().all())


async def settlement_for(
    session: AsyncSession, order: FxOrder
) -> Settlement | None:
    result = await session.execute(
        select(Settlement)
        .join(SettlementAllocation)
        .where(SettlementAllocation.fx_order_id == order.id)
    )
    return result.scalars().first()


__all__ = [
    "TOLERANCE_USDT",
    "Line",
    "SettlementError",
    "allocations_of",
    "already_settled",
    "discrepancy",
    "expected_total",
    "is_material",
    "record",
    "settlement_for",
]
