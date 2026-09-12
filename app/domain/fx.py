"""FX deals: the state machine and the figures.

Specified by NexterPay on 12 September, expanding the FX Flow note of
5 September. The route:

    1. client asks for a rate            free format, their group
    2. we ask a supplier                 free format, their group
    3. supplier quotes us                no good -> back to 2
    4. we give the client the rate       they confirm, then say what they want
    5. we create the client's order      amount, rate, what they receive, name
    6. client confirms                   the order is registered
    7. we create the supplier's order    same figures on their side
    8. supplier accepts
    9. chasing                           can be days
   10. supplier confirms the hash
   11. we pass it to the client
   12. client confirms receipt           closed

Two return paths, not one: we can reject the supplier's rate at 3, and the
client can reject ours at 4. Both go back rather than killing the deal, so the
second quote is a new offer on the same order rather than a new order that has
lost its history.

**The safety property this module exists to hold.** Every figure on a deal
exists twice - our rate and the supplier's, what the client pays and what the
supplier receives - and the difference between the two rates is NexterPay's
margin. A supplier rate reaching a client is the only failure in this system
that costs money rather than goodwill.

So there is no function here that takes "the rate". Everything is named for
its side, `client_view` and `supplier_view` are the only things that compose
figures for a counterparty, and each reads one side's columns and cannot see
the other's. A leak needs somebody to call the wrong function, not merely to
forget which number they were holding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Client, Event, FxOrder, FxReferenceCounter, WorkItem
from app.domain.enums import EventType, FxOrderStatus, FxSide, StaffRole
from app.domain.errors import DomainError
from app.domain.work_items import Actor

# Creating an order commits NexterPay to a price. It is not a note.
ROLE_REQUIRED_TO_QUOTE = StaffRole.OPERATOR
ROLE_REQUIRED_TO_CREATE_ORDER = StaffRole.OPERATOR
ROLE_REQUIRED_TO_RECORD_HASH = StaffRole.OPERATOR

# Tron only. Ethereum is phase two, agreed 7 September.
EXPLORERS = {"tron": "https://tronscan.org/#/transaction/{hash}"}


class FxError(DomainError):
    """Something was asked of a deal that its current state does not allow."""


# --------------------------------------------------------------------------
# Parsing what a member of staff typed
# --------------------------------------------------------------------------

def parse_amount(text: str) -> Decimal:
    """A figure typed by a person, into something arithmetic can be done with.

    Deliberately strict about what it accepts and deliberately generous about
    formatting. "1,250,000.50" and "1250000.50" are the same number and both
    get typed; "about 1.2m" is not a number and must not become one, because
    the value is going to a counterparty as a commitment.

    Floats are never involved. 0.1 + 0.2 is not 0.3 in binary floating point,
    and this is somebody's money.
    """
    cleaned = (text or "").strip().replace(",", "").replace(" ", "")
    if not cleaned:
        raise FxError("That is empty. Give me a number.")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise FxError(
            f"“{text.strip()}” is not a number I can use. Digits and a decimal "
            f"point only - no currency symbols, no words."
        )
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise FxError(f"“{text.strip()}” is not a number I can use.") from None
    if value <= 0:
        raise FxError("That has to be more than zero.")
    return value


def parse_rate(text: str) -> Decimal:
    """A rate. Same rules as an amount; named separately because the two are
    never interchangeable and the error message should say which was wanted."""
    try:
        return parse_amount(text)
    except FxError as exc:
        raise FxError(str(exc).replace("number", "rate", 1)) from None


def format_money(value: Decimal | None) -> str:
    """For display. Thousands separated, trailing zeros trimmed.

    A rate of 1.1642000000 reads as noise; 1.1642 reads as a rate. But a
    trailing zero that is part of the figure - 1250.50 - stays, because
    money written as 1250.5 looks like a typo to anyone in finance.
    """
    if value is None:
        return "—"
    normalised = value.normalize()
    # normalize() turns 1000 into 1E+3. Quantising it back keeps large round
    # amounts readable as amounts rather than as scientific notation.
    exponent = normalised.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        normalised = normalised.quantize(Decimal(1))
    text = f"{normalised:,f}"
    if "." in text:
        whole, _, fraction = text.partition(".")
        if len(fraction) == 1:
            text = f"{whole}.{fraction}0"
    return text


def explorer_link(chain: str, tx_hash: str) -> str | None:
    template = EXPLORERS.get((chain or "").lower())
    return template.format(hash=tx_hash) if template else None


# --------------------------------------------------------------------------
# What each side is allowed to see
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderView:
    """The figures for one counterparty, and nothing else.

    Built by `view_for`, which is the only function that composes an FX order
    for something outside NexterPay. It takes a side and reads that side's
    columns; there is no path from here to the other side's rate.
    """

    reference: str
    account_name: str | None
    rate: Decimal | None
    pays: Decimal | None
    pays_currency: str | None
    receives: Decimal | None
    receives_currency: str | None

    def lines(self) -> list[str]:
        out = [f"Order {self.reference}"]
        if self.account_name:
            out.append(f"Account: {self.account_name}")
        if self.rate is not None:
            out.append(f"Rate: {format_money(self.rate)}")
        if self.pays is not None:
            out.append(
                f"You send: {format_money(self.pays)} {self.pays_currency or ''}".rstrip()
            )
        if self.receives is not None:
            out.append(
                f"You receive: {format_money(self.receives)} "
                f"{self.receives_currency or ''}".rstrip()
            )
        return out


def view_for(order: FxOrder, side: FxSide) -> OrderView:
    """The one way figures leave NexterPay.

    The reference matters as much as the numbers. A client is shown
    `client_reference`, which never carries the supplier code - a client who
    can see which supplier their deal sits with can work out who NexterPay buy
    from, which is the first step to working out the margin.
    """
    if side is FxSide.CLIENT:
        return OrderView(
            reference=order.client_reference,
            account_name=order.client_account_name,
            rate=order.client_rate,
            pays=order.client_pays,
            pays_currency=order.client_pays_currency,
            receives=order.client_receives,
            receives_currency=order.client_receives_currency,
        )
    return OrderView(
        reference=order.display_reference,
        account_name=order.supplier_account_name,
        rate=order.supplier_rate,
        pays=order.supplier_pays,
        pays_currency=order.supplier_pays_currency,
        receives=order.supplier_receives,
        receives_currency=order.supplier_receives_currency,
    )


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------

async def record_event(
    session: AsyncSession,
    order: FxOrder,
    event_type: EventType,
    actor: Actor,
    **payload,
) -> Event:
    """Every change to a deal, against the work item it is conducted through.

    Against the client-side work item deliberately, so that the history of the
    deal sits in one place and `/nphistory` on the client request shows the
    whole thing rather than half of it.

    Figures go into the payload as strings. A Decimal is not JSON, and a float
    would quietly round somebody's money in the audit log - which is the one
    place it must not be rounded.
    """
    cleaned = {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in payload.items()
    }
    event = Event(
        work_item_id=order.client_work_item_id,
        event_type=event_type,
        actor_staff_id=actor.staff.id if actor.staff else None,
        actor_telegram_user_id=actor.telegram_user_id,
        actor_name=actor.name,
        payload={"fx_reference": order.display_reference, **cleaned},
    )
    session.add(event)
    await session.flush()
    return event


async def _next_reference(session: AsyncSession) -> int:
    counter = await session.get(FxReferenceCounter, 1, with_for_update=True)
    if counter is None:
        counter = FxReferenceCounter(id=1, next_value=1000)
        session.add(counter)
        await session.flush()
    value = counter.next_value
    counter.next_value = value + 1
    await session.flush()
    return value


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------

# What may follow what. Written out rather than inferred, because "a hash
# should not be issuable before a rate is approved" was a decision NexterPay
# took knowingly on 5 September, and its cost is real: somebody who settles a
# deal on a phone call has to record the steps afterwards rather than jumping
# to the end. A table makes that cost visible instead of scattering it
# through a dozen if-statements.
ALLOWED: dict[FxOrderStatus, tuple[FxOrderStatus, ...]] = {
    FxOrderStatus.RATE_REQUESTED: (FxOrderStatus.RATE_QUOTED,),
    FxOrderStatus.RATE_QUOTED: (
        FxOrderStatus.RATE_REJECTED,
        FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
    ),
    # Rejection returns to the beginning: we go back to the supplier for
    # another price, and the next quote is a new offer on the same deal.
    FxOrderStatus.RATE_REJECTED: (
        FxOrderStatus.RATE_REQUESTED,
        FxOrderStatus.RATE_QUOTED,
    ),
    FxOrderStatus.AWAITING_CLIENT_CONFIRMATION: (
        FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
        # A client may still walk away between being sent the order and
        # confirming it. Better a return path than somebody closing the deal
        # and opening a new one, which loses the link between the two quotes.
        FxOrderStatus.RATE_REJECTED,
    ),
    FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE: (FxOrderStatus.AWAITING_SETTLEMENT,),
    FxOrderStatus.AWAITING_SETTLEMENT: (FxOrderStatus.AWAITING_RECEIPT,),
    FxOrderStatus.AWAITING_RECEIPT: (FxOrderStatus.CLOSED,),
    FxOrderStatus.CLOSED: (),
}


def may_move(current: FxOrderStatus, target: FxOrderStatus) -> bool:
    return target in ALLOWED.get(current, ())


def _require_state(order: FxOrder, *allowed: FxOrderStatus) -> None:
    if order.status not in allowed:
        wanted = " or ".join(s.label for s in allowed)
        raise FxError(
            f"{order.display_reference} is {order.status.label.lower()}, "
            f"and that step needs it to be {wanted.lower()}."
        )


def _move(order: FxOrder, target: FxOrderStatus) -> None:
    if not may_move(order.status, target):
        raise FxError(
            f"{order.display_reference} cannot go from {order.status.label.lower()} "
            f"to {target.label.lower()}."
        )
    order.status = target


# --------------------------------------------------------------------------
# The steps
# --------------------------------------------------------------------------

async def open_order(
    session: AsyncSession,
    *,
    client: Client,
    client_work_item: WorkItem,
    actor: Actor,
) -> FxOrder:
    """Step 1. A client has asked about a rate.

    The deal starts against the request already open in their group, so the
    conversation that produced it stays attached to it.
    """
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    order = FxOrder(
        reference=await _next_reference(session),
        client_id=client.id,
        client_code=client.code,
        client_work_item_id=client_work_item.id,
        client_account_name=client.name,
        status=FxOrderStatus.RATE_REQUESTED,
    )
    session.add(order)
    await session.flush()
    await record_event(session, order, EventType.FX_RATE_REQUESTED, actor)
    return order


async def record_supplier_quote(
    session: AsyncSession,
    order: FxOrder,
    *,
    supplier: Client,
    supplier_work_item: WorkItem | None,
    rate: Decimal,
    actor: Actor,
) -> FxOrder:
    """Step 3. What the supplier quoted us. Internal, always."""
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    _require_state(
        order, FxOrderStatus.RATE_REQUESTED, FxOrderStatus.RATE_REJECTED
    )
    order.supplier_id = supplier.id
    order.supplier_code = supplier.code
    order.supplier_rate = rate
    if supplier_work_item is not None:
        order.supplier_work_item_id = supplier_work_item.id
    await record_event(
        session, order, EventType.FX_SUPPLIER_QUOTED, actor,
        supplier=supplier.name, rate=rate,
    )
    await session.flush()
    return order


async def reject_supplier_rate(
    session: AsyncSession, order: FxOrder, *, reason: str, actor: Actor
) -> FxOrder:
    """Step 3, the other way. NexterPay: "we sometimes have to say to supplier
    its no good". Stays in the same state - we are still waiting on a price."""
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    _require_state(order, FxOrderStatus.RATE_REQUESTED, FxOrderStatus.RATE_REJECTED)
    rejected = order.supplier_rate
    order.supplier_rate = None
    await record_event(
        session, order, EventType.FX_SUPPLIER_RATE_REJECTED, actor,
        rate=rejected, reason=reason,
    )
    await session.flush()
    return order


async def quote_client(
    session: AsyncSession, order: FxOrder, *, rate: Decimal, actor: Actor
) -> FxOrder:
    """Step 4. Our rate, which is the supplier's plus NexterPay's margin.

    The margin is never computed here and never stored as a figure of its own.
    It is the difference between two rates that are each recorded for their own
    reason, which means there is no "margin" field for anything to render by
    accident.
    """
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    _require_state(
        order,
        FxOrderStatus.RATE_REQUESTED,
        FxOrderStatus.RATE_QUOTED,
        FxOrderStatus.RATE_REJECTED,
    )
    if order.supplier_rate is not None and rate < order.supplier_rate:
        # Not forbidden - there are reasons to quote at or under cost - but it
        # should be a decision rather than a typo, and a typo is far likelier.
        raise FxError(
            f"That quote is below the supplier's rate, so the deal would lose "
            f"money. If that is deliberate, say so in the topic first."
        )
    order.client_rate = rate
    if order.status is not FxOrderStatus.RATE_QUOTED:
        _move(order, FxOrderStatus.RATE_QUOTED)
    await record_event(session, order, EventType.FX_RATE_QUOTED, actor, rate=rate)
    await session.flush()
    return order


async def reject_client_rate(
    session: AsyncSession, order: FxOrder, *, reason: str, actor: Actor
) -> FxOrder:
    """Step 4, the other way. The client's own words on why, kept verbatim -
    "too high" and "we have a better price elsewhere" lead to different next
    conversations."""
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    _require_state(
        order,
        FxOrderStatus.RATE_QUOTED,
        FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
    )
    _move(order, FxOrderStatus.RATE_REJECTED)
    await record_event(
        session, order, EventType.FX_RATE_REJECTED, actor,
        rate=order.client_rate, reason=reason,
    )
    await session.flush()
    return order


async def create_client_order(
    session: AsyncSession,
    order: FxOrder,
    *,
    account_name: str,
    rate: Decimal,
    pays: Decimal,
    pays_currency: str,
    receives: Decimal,
    receives_currency: str,
    actor: Actor,
) -> FxOrder:
    """Step 5. The order the client is asked to confirm.

    All three figures together, because they are one offer: a rate is a quote
    for an amount, and changing the amount changes the rate. Splitting them
    would let somebody confirm half a deal.
    """
    actor.require(ROLE_REQUIRED_TO_CREATE_ORDER)
    _require_state(
        order, FxOrderStatus.RATE_QUOTED, FxOrderStatus.AWAITING_CLIENT_CONFIRMATION
    )
    order.client_account_name = account_name.strip()
    order.client_rate = rate
    order.client_pays = pays
    order.client_pays_currency = pays_currency.strip().upper()
    order.client_receives = receives
    order.client_receives_currency = receives_currency.strip().upper()
    if order.status is not FxOrderStatus.AWAITING_CLIENT_CONFIRMATION:
        _move(order, FxOrderStatus.AWAITING_CLIENT_CONFIRMATION)
    await record_event(
        session, order, EventType.FX_ORDER_CREATED, actor,
        side=FxSide.CLIENT.value, account=order.client_account_name,
        rate=rate, pays=pays, pays_currency=order.client_pays_currency,
        receives=receives, receives_currency=order.client_receives_currency,
    )
    await session.flush()
    return order


async def client_confirms(
    session: AsyncSession, order: FxOrder, *, actor: Actor
) -> FxOrder:
    """Step 6. The figures are fixed from here; nothing above this line changes
    without a new quote."""
    _require_state(order, FxOrderStatus.AWAITING_CLIENT_CONFIRMATION)
    order.client_confirmed_at = utcnow()
    _move(order, FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE)
    await record_event(
        session, order, EventType.FX_CLIENT_CONFIRMED, actor,
        rate=order.client_rate, pays=order.client_pays,
        receives=order.client_receives,
    )
    await session.flush()
    return order


async def create_supplier_order(
    session: AsyncSession,
    order: FxOrder,
    *,
    account_name: str,
    rate: Decimal,
    pays: Decimal,
    pays_currency: str,
    receives: Decimal,
    receives_currency: str,
    actor: Actor,
) -> FxOrder:
    """Step 7. The same act on the other side.

    `account_name` here is NexterPay's account code with that supplier -
    "Nexterpay7" - not the client's name. It is the field that keeps the
    client's identity off the supplier's side of the deal, in the same way the
    reference keeps the supplier's code off the client's.
    """
    actor.require(ROLE_REQUIRED_TO_CREATE_ORDER)
    _require_state(order, FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE)
    order.supplier_account_name = account_name.strip()
    order.supplier_rate = rate
    order.supplier_pays = pays
    order.supplier_pays_currency = pays_currency.strip().upper()
    order.supplier_receives = receives
    order.supplier_receives_currency = receives_currency.strip().upper()
    await record_event(
        session, order, EventType.FX_ORDER_CREATED, actor,
        side=FxSide.SUPPLIER.value, account=order.supplier_account_name,
        rate=rate, pays=pays, pays_currency=order.supplier_pays_currency,
        receives=receives, receives_currency=order.supplier_receives_currency,
    )
    await session.flush()
    return order


async def supplier_accepts(
    session: AsyncSession, order: FxOrder, *, actor: Actor
) -> FxOrder:
    """Step 8. After this we are chasing, which is an action rather than a
    state - it happens inside Awaiting settlement and is recorded as messages."""
    _require_state(order, FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE)
    order.supplier_confirmed_at = utcnow()
    _move(order, FxOrderStatus.AWAITING_SETTLEMENT)
    await record_event(session, order, EventType.FX_SUPPLIER_ACCEPTED, actor)
    await session.flush()
    return order


async def record_hash(
    session: AsyncSession, order: FxOrder, *, tx_hash: str, actor: Actor
) -> FxOrder:
    """Step 10. The supplier has settled.

    Not the end. NexterPay, 12 September: we pass it to the client, and the
    deal closes when the client confirms receipt - not when the money moves.
    """
    actor.require(ROLE_REQUIRED_TO_RECORD_HASH)
    _require_state(order, FxOrderStatus.AWAITING_SETTLEMENT)
    cleaned = (tx_hash or "").strip()
    if not cleaned:
        raise FxError("A settlement needs a hash.")
    if len(cleaned) < 16 or " " in cleaned:
        raise FxError(
            f"“{cleaned}” does not look like a transaction hash. It goes to the "
            f"client as proof, so it is worth pasting again."
        )
    order.tx_hash = cleaned
    order.settled_at = utcnow()
    _move(order, FxOrderStatus.AWAITING_RECEIPT)
    await record_event(session, order, EventType.FX_HASH_RECORDED, actor, tx_hash=cleaned)
    await session.flush()
    return order


async def client_confirms_receipt(
    session: AsyncSession, order: FxOrder, *, actor: Actor
) -> FxOrder:
    """Step 12. The client has the funds, and that is what closes the deal."""
    _require_state(order, FxOrderStatus.AWAITING_RECEIPT)
    order.closed_at = utcnow()
    _move(order, FxOrderStatus.CLOSED)
    await record_event(session, order, EventType.FX_RECEIPT_CONFIRMED, actor)
    await session.flush()
    return order


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

async def by_reference(session: AsyncSession, reference: int) -> FxOrder | None:
    result = await session.execute(
        select(FxOrder).where(FxOrder.reference == reference)
    )
    return result.scalar_one_or_none()


def parse_fx_reference(text: str) -> int | None:
    """`FXACME-1042`, `FXACME-SPEX-1042`, `FX#1042` and `1042` all mean 1042."""
    if not text:
        return None
    match = re.search(r"(\d{3,})\s*$", text.strip())
    return int(match.group(1)) if match else None


async def open_orders(session: AsyncSession) -> list[FxOrder]:
    result = await session.execute(
        select(FxOrder)
        .where(FxOrder.status != FxOrderStatus.CLOSED)
        .order_by(FxOrder.reference)
    )
    return list(result.scalars())
