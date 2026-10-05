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
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    FxOrder,
    Settlement,
    SettlementAllocation,
    SettlementReferenceCounter,
)
from app.domain import accounts, corridors
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

# USDT has six decimal places on Tron, so that is the precision a USDT figure
# can actually have. Dividing a local amount by a rate does not respect that -
# 3000000/606 recurs - and an unrounded Decimal carries twenty-eight
# significant digits into anything that renders it. The desk saw
# "39,309.46940847930946940847931 USDT" in a preview before this was added,
# which is both unreadable and a false precision about somebody's money.
USDT_PLACES = Decimal("0.000001")


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
        return (self.local_amount / self.rate).quantize(
            USDT_PLACES, rounding=ROUND_HALF_UP
        )

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


# --------------------------------------------------------------------------
# Matching a pasted block to the deals it is about
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Match:
    """One pasted line, and the order it belongs to - or why it does not.

    A line that cannot be matched is carried rather than dropped. A settlement
    where three of four lines were understood and the fourth vanished silently
    is the worst possible outcome: the desk sees a plausible total and a
    client waits for money against a deal nobody recorded.
    """

    line_number: int
    parsed: object            # settlement_text.ParsedLine
    order: FxOrder | None
    problem: str | None = None

    @property
    def matched(self) -> bool:
        return self.order is not None and self.problem is None


def local_leg(order: FxOrder) -> tuple[str | None, Decimal | None]:
    """What the supplier sends out, and the currency it is in.

    This exists because reading the wrong one of these two columns is a
    mistake the platform has already made, in two separate places, and shipped.

    An order has four money columns. `supplier_pays` is what the supplier
    sends - the local currency, paid out to the beneficiary - and
    `supplier_receives` is the USDT we send them for it. The order flow fills
    them from two questions in that order: "what does the supplier send?" then
    "what do they receive?".

    A settlement line is about the first of those. `XOF: 20130000/585` says
    the supplier paid out 20,130,000 XOF; the USDT on the line is that figure
    divided by the rate, which is what we owe them. Matching a line against
    `supplier_receives` compares XOF to USDT and finds nothing, for ever.

    Found on 5 October by building a deal through the platform's own flow and
    then settling it: the line matched the order exactly and the platform said
    "no open deal for that amount". 1,120 tests passed over it, because every
    fixture set `supplier_receives` to the local amount by hand - an order the
    platform itself cannot produce.

    The currency comes back with the amount rather than being read separately,
    which is the actual lesson: these two belong together, and every caller
    that split them got it wrong. `currency_code` is the fallback only for
    rows written before the per-leg currency existed.
    """
    currency = order.supplier_pays_currency or order.currency_code
    return (currency.upper() if currency else None), order.supplier_pays


async def _settleable_orders(session: AsyncSession) -> list[FxOrder]:
    result = await session.execute(
        select(FxOrder)
        .where(FxOrder.status == FxOrderStatus.AWAITING_SETTLEMENT)
        .order_by(FxOrder.reference)
    )
    return list(result.scalars().all())


async def match_lines(session: AsyncSession, parsed_lines: list) -> list[Match]:
    """Work out which open deal each pasted line is about.

    Matched on the currency and the local amount the supplier is sending,
    because that is all their lines carry - there is no reference on them.
    `CI - 50250000/583` says fifty million two hundred and fifty thousand XOF,
    and if exactly one deal awaiting settlement is for that, the line is about
    that deal.

    Deliberately refuses to choose when two deals fit. Two clients sending the
    same amount in the same currency on the same day is not rare on a desk
    doing volume, and picking one would attach a payment to the wrong client's
    deal - which is a client told their money has arrived when it has not, and
    another left waiting with the platform insisting they were paid.

    Each order is claimed by at most one line, so two identical lines in one
    block do not both land on the same deal.
    """
    candidates = await _settleable_orders(session)
    taken: set[int] = set()
    matches: list[Match] = []

    for number, line in enumerate(parsed_lines, start=1):
        currency = line.currency_code
        if currency is None:
            matches.append(Match(number, line, None, "unknown currency"))
            continue

        fits = []
        for order in candidates:
            if order.id in taken:
                continue
            order_currency, order_amount = local_leg(order)
            if order_currency != currency or order_amount is None:
                continue
            if order_amount == line.local_amount:
                fits.append(order)

        # The account number on the line, where there is one, narrows further.
        #
        # NexterPay, 4 October: "BBS is a big supplier, so we have multiple
        # accounts. BBS number is Nexterpay Number." It narrows rather than
        # decides, because the accounts are shared - BBS 1 carries both
        # LuckyStar and Spayz Category B - so this cuts the candidates down
        # and the ambiguity check below still has the last word.
        #
        # Applied only when it leaves something. An account we hold but cannot
        # resolve, or one belonging to a client not registered here yet, must
        # not turn a line that would have matched on amount into one that
        # matches nothing: the mapping is there to help, and a half-loaded
        # mapping that started hiding deals would be worse than none.
        if len(fits) > 1 and getattr(line, "account", None):
            supplier_code = next(
                (o.supplier_code for o in fits if o.supplier_code), None
            )
            if supplier_code:
                allowed = await accounts.clients_on_account(
                    session, supplier_code=supplier_code, number=line.account
                )
                if allowed:
                    narrowed = [
                        order for order in fits
                        if order.client_id in {client.id for client in allowed}
                    ]
                    if narrowed:
                        fits = narrowed

        if not fits:
            matches.append(
                Match(number, line, None, "no open deal for that amount")
            )
        elif len(fits) > 1:
            matches.append(
                Match(
                    number, line, None,
                    f"{len(fits)} open deals are for that amount - "
                    f"{', '.join(o.display_reference for o in fits)}",
                )
            )
        else:
            taken.add(fits[0].id)
            matches.append(Match(number, line, fits[0]))

    return matches


def lines_from(matches: list[Match]) -> list[Line]:
    """The matched rows, as something `record` can take.

    The country is taken from the line when it names one and from the order
    when it does not - their older blocks label by currency, and XOF names
    eight countries, so the order is the only thing that knows which.
    """
    built = []
    for match in matches:
        if not match.matched:
            continue
        country = match.parsed.country_code or match.order.country_code
        built.append(
            Line(
                order=match.order,
                country_code=country,
                local_amount=match.parsed.local_amount,
                rate=match.parsed.rate,
            )
        )
    return built


__all__ = [
    "TOLERANCE_USDT",
    "USDT_PLACES",
    "Match",
    "lines_from",
    "match_lines",
    "Line",
    "SettlementError",
    "allocations_of",
    "already_settled",
    "attach_hash",
    "awaiting_hash",
    "discrepancy",
    "expected_total",
    "is_material",
    "local_leg",
    "record",
    "settlement_for",
]


# --------------------------------------------------------------------------
# A hash that arrives after the settlement
# --------------------------------------------------------------------------

async def awaiting_hash(session: AsyncSession) -> list[Settlement]:
    """Settlements recorded without proof of payment, newest first.

    This is a normal state rather than an error. Suppliers send the lines and
    the hash as two messages - it happened in their own chat on 1 September,
    the lines at 17:49 and the hash afterwards - so a desk working at the
    speed of the conversation will often record one before the other arrives.
    """
    result = await session.execute(
        select(Settlement)
        .where(Settlement.tx_hash.is_(None))
        .order_by(Settlement.reference.desc())
    )
    return list(result.scalars().all())


async def attach_hash(
    session: AsyncSession,
    settlement: Settlement,
    *,
    tx_hash: str,
    actor: Actor,
) -> Settlement:
    """Put the proof against a payment already recorded.

    The hash goes onto every order the settlement covers as well as the
    settlement itself, because that is where a client asking about their own
    deal will be shown it. Doing only one of the two would leave a client
    being told there is no proof of a payment the desk can see the proof of.

    Refuses to overwrite. A settlement that already has a hash and is offered
    a different one is either a mistake or two payments being confused, and
    both want a person rather than a quiet replacement.
    """
    if settlement.tx_hash:
        raise SettlementError(
            f"{settlement.display_reference} already has a hash. If the "
            f"payment is a different one, it is a different settlement."
        )

    cleaned = check_hash(tx_hash)
    settlement.tx_hash = cleaned

    for allocation in await allocations_of(session, settlement):
        order = await session.get(FxOrder, allocation.fx_order_id)
        if order is None:
            continue
        order.tx_hash = cleaned
        await record_event(
            session, order, EventType.FX_HASH_RECORDED, actor,
            tx_hash=cleaned,
            settlement=settlement.display_reference,
            attached_later=True,
        )

    await session.flush()
    return settlement
