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
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
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


def parse_currency_code(text: str) -> str:
    """A three-letter currency code, upper-cased.

    Strict about the length because NexterPay's clients read it as a fact -
    "rate on INR is 89.50" - and "rate on Indian Rupees is 89.50" is a
    different sentence somebody has to check. Three letters is also the whole
    of ISO 4217, so nothing legitimate is being refused.
    """
    cleaned = (text or "").strip().upper()
    if not cleaned:
        raise FxError("Which currency? Three letters, like INR.")
    if not (len(cleaned) == 3 and cleaned.isalpha()):
        raise FxError(
            f"“{text.strip()}” is not a three-letter currency code. INR, NGN, "
            f"PHP - three letters, nothing else."
        )
    return cleaned


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


def check_hash(text: str) -> str:
    """A transaction hash, or a clear refusal.

    Pulled out of `record_hash` so the same check can run the moment somebody
    pastes one, while the real hash is still on their clipboard. A truncated
    paste caught at the end of a flow means finding it again; caught on entry
    it means pressing ctrl+v twice.

    Deliberately shallow - length and no whitespace. Tron and Ethereum hashes
    differ in shape, chains get added, and a strict pattern would start
    refusing valid hashes the first time NexterPay settle somewhere new. What
    it catches is the mistake that actually happens: half a hash.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise FxError("A settlement needs a hash.")
    if len(cleaned) < 16 or " " in cleaned:
        raise FxError(
            f"“{cleaned}” does not look like a transaction hash. It goes to the "
            f"client as proof, so it is worth pasting again."
        )
    return cleaned


def explorer_link(chain: str, tx_hash: str) -> str | None:
    """A link to look the payment up, or None when there is nothing to look up.

    The empty-hash guard is not defensive tidiness. A settlement pasted as a
    block often has no hash on it - their supplier sends the figures and the
    proof as two messages - and without this the client was sent
    `https://tronscan.org/#/transaction/` on its own: a live link to a page
    about nothing, under the words "has been settled". Seen on 6 October, in
    the first settlement notice a client ever received.
    """
    template = EXPLORERS.get((chain or "").lower())
    if not template or not (tx_hash or "").strip():
        return None
    return template.format(hash=tx_hash.strip())


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
    currency_code: str | None
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

    The reference matters as much as the numbers, in both directions. A client
    is shown `client_reference`, which never carries the supplier code - a
    client who can see which supplier their deal sits with can work out who
    NexterPay buy from. A supplier is shown `supplier_reference`, which never
    carries the client's, for the mirror reason: a supplier who knows the
    client and the volume can work out most of the margin.

    `display_reference` - FXACME-SPEX-1042 - belongs to the Operations topic
    and appears here for neither side.
    """
    if side is FxSide.CLIENT:
        return OrderView(
            reference=order.client_reference,
            account_name=order.client_account_name,
            # Not a side's property - both halves of a deal are priced in the
            # same local currency, which is the whole reason it sits on the
            # order. Carried here so nothing outside has to reach past the view
            # to build a sentence about a rate.
            currency_code=order.currency_code,
            rate=order.client_rate,
            pays=order.client_pays,
            pays_currency=order.client_pays_currency,
            receives=order.client_receives,
            receives_currency=order.client_receives_currency,
        )
    return OrderView(
        reference=order.supplier_reference,
        account_name=order.supplier_account_name,
        currency_code=order.currency_code,
        rate=order.supplier_rate,
        pays=order.supplier_pays,
        pays_currency=order.supplier_pays_currency,
        receives=order.supplier_receives,
        receives_currency=order.supplier_receives_currency,
    )


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------

async def has_event(
    session: AsyncSession, order: FxOrder, event_type: EventType
) -> bool:
    """Has this already happened to this deal?

    The idempotency check for the steps that deliberately do not move the
    deal's status. Where the status does move, `_require_state` is the better
    guard and this is not needed; where it does not, this is the only thing
    that can tell a second answer from a first.

    Matched on the deal's reference as well as the work item, because a client
    request can carry more than one deal and the events all live against that
    one work item - so "has this been accepted" has to mean this deal rather
    than any deal on the request.

    The reference is matched in Python rather than in the query. `payload` is a
    generic JSON column, and querying inside it is one of the few things that
    genuinely differs between SQLite and Postgres - which is the divergence
    this project is least able to see, since the tests run on one and the
    clients on the other. The rows here are the events of one type on one
    request; there is nothing to gain by being clever with them.
    """
    result = await session.execute(
        select(Event).where(
            Event.work_item_id == order.client_work_item_id,
            Event.event_type == event_type,
        )
    )
    reference = order.display_reference
    return any(
        (event.payload or {}).get("fx_reference") == reference
        for event in result.scalars().all()
    )


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
    FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE: (
        FxOrderStatus.AWAITING_SETTLEMENT,
        # Repricing. NexterPay, 4 October: "we have had occasions where after
        # a deal is agreed, stock issues cause rates to change, they wont
        # settle on that rate, they will notify us of change before we agree
        # it with client."
        #
        # A new price is a new offer, so the deal goes back to being quoted
        # and the client agrees it or does not. Note that an *amount* change
        # does not come through here at all - that one the desk decides and
        # the status does not move.
        FxOrderStatus.RATE_QUOTED,
    ),
    FxOrderStatus.AWAITING_SETTLEMENT: (
        FxOrderStatus.AWAITING_RECEIPT,
        # The same return path, and this is the state it is usually taken
        # from: the supplier comes to settle and says the rate has moved.
        FxOrderStatus.RATE_QUOTED,
    ),
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
    session: AsyncSession, order: FxOrder, *, rate: Decimal, actor: Actor,
    currency_code: str | None = None,
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
            "That quote is below the supplier's rate, so the deal would lose "
            "money. If that is deliberate, say so in the topic first."
        )
    order.client_rate = rate
    if currency_code:
        order.currency_code = currency_code
    if order.status is not FxOrderStatus.RATE_QUOTED:
        _move(order, FxOrderStatus.RATE_QUOTED)
    await record_event(
        session, order, EventType.FX_RATE_QUOTED, actor,
        rate=rate, currency=order.currency_code,
    )
    await session.flush()
    return order


async def client_accepts_rate(
    session: AsyncSession, order: FxOrder, *, actor: Actor
) -> FxOrder:
    """Step 4b. The client has said yes to the rate, before any figures exist.

    NexterPay, 16 September: the client is shown "rate on INR is 89.50" and
    asked whether to proceed, and the order is built afterwards.

    Deliberately does not move the deal. Saying yes to a price is not the same
    as agreeing an order - there are no amounts yet, and the desk still has to
    build one. Moving the status here would leave a deal reading Awaiting
    client confirmation with nothing for the client to confirm.

    It is also a different event from `client_confirms`, which is the client
    agreeing to figures. Six weeks later, a dispute turns on which of those two
    promises was actually given, so the history has to be able to tell them
    apart.

    **Answering twice is refused, and the guard has to be the event rather than
    the status.** Everywhere else on this deal, doing something twice is caught
    by `_require_state` - the first action moves the deal and the second finds
    the wrong state. This one deliberately does not move the deal, so that
    check can never fire, and the client could tap Yes as many times as they
    liked and be thanked each time. NexterPay's tester did exactly that on
    29 September, a minute apart, and got two "we will send the order through
    shortly".
    """
    _require_state(order, FxOrderStatus.RATE_QUOTED)
    if await has_event(session, order, EventType.FX_RATE_ACCEPTED):
        raise FxError(
            f"{order.display_reference} has already been accepted at that rate."
        )
    await record_event(
        session, order, EventType.FX_RATE_ACCEPTED, actor,
        rate=order.client_rate, currency=order.currency_code,
    )
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


# The states an order's figures can still change in.
#
# Everything after the client has been sent an order and before the money has
# moved. Earlier there is nothing agreed to change; once a settlement exists
# the figures are a record of what was paid rather than what was agreed, and
# that is not ours to rewrite.
AMENDABLE = (
    FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
    FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
    FxOrderStatus.AWAITING_SETTLEMENT,
)


async def amend_amount(
    session: AsyncSession,
    order: FxOrder,
    *,
    client_pays: Decimal,
    client_receives: Decimal,
    supplier_pays: Decimal | None = None,
    supplier_receives: Decimal | None = None,
    reason: str,
    actor: Actor,
) -> FxOrder:
    """The supplier could not fund the whole amount, so the amount comes down.

    NexterPay, 3 October: "if the supplier does not have enough, the order
    amount may change." Asked whether the client has to agree the new figure,
    Jason was unambiguous on 4 October: **"No we make the decision on the
    short."**

    So the deal does not move. It stays exactly where it was - awaiting the
    supplier, or awaiting settlement - and the client is told rather than
    asked. That is NexterPay's call to make and the platform's job is to
    record it, not to invent an approval step they do not want.

    This was built the other way round first, on the assumption that a client
    whose receipt had changed would need to agree it. That assumption was
    wrong, and it is worth leaving written down: the agreement NexterPay have
    with their clients is not derivable from the figures.

    The rate is untouched here on purpose. A rate that moves is a different
    event with the opposite handling - see `reprice`.
    """
    actor.require(ROLE_REQUIRED_TO_CREATE_ORDER)
    _require_state(order, *AMENDABLE)

    cleaned = (reason or "").strip()
    if not cleaned:
        raise FxError(
            "An amendment needs a reason. Say why the amount changed - the "
            "supplier being short reads differently from a correction."
        )
    if client_pays <= 0 or client_receives <= 0:
        raise FxError("An amended order still has to be for something.")

    before = {
        "client_pays": order.client_pays,
        "client_receives": order.client_receives,
        "supplier_pays": order.supplier_pays,
        "supplier_receives": order.supplier_receives,
    }

    order.client_pays = client_pays
    order.client_receives = client_receives
    if supplier_pays is not None:
        order.supplier_pays = supplier_pays
    if supplier_receives is not None:
        order.supplier_receives = supplier_receives

    await record_event(
        session, order, EventType.FX_ORDER_AMENDED, actor,
        kind="amount",
        reason=cleaned,
        was_client_pays=before["client_pays"],
        was_client_receives=before["client_receives"],
        was_supplier_pays=before["supplier_pays"],
        was_supplier_receives=before["supplier_receives"],
        client_pays=client_pays,
        client_receives=client_receives,
        supplier_pays=order.supplier_pays,
        supplier_receives=order.supplier_receives,
    )
    await session.flush()
    return order


async def reprice(
    session: AsyncSession,
    order: FxOrder,
    *,
    supplier_rate: Decimal,
    client_rate: Decimal,
    reason: str,
    actor: Actor,
) -> FxOrder:
    """The supplier has changed the rate after the deal was agreed.

    NexterPay, 4 October: "we have had occasions where after a deal is
    agreed, stock issues cause rates to change, they wont settle on that
    rate, they will notify us of change before we agree it with client."

    The opposite handling to `amend_amount`, and the opposite of what this
    module assumed first. A price is the one thing the client agreed to, so a
    new price is a new offer: the deal goes back to being quoted and the
    client accepts it or does not. The desk does not get to decide this one
    on their behalf.

    Both rates are taken together, as everywhere else in this module. The
    supplier's new rate is what prompted the change and ours is what the
    client will be asked about, and setting one without the other would leave
    a deal whose margin is nonsense until somebody remembers to finish.
    """
    actor.require(ROLE_REQUIRED_TO_QUOTE)
    _require_state(order, *AMENDABLE)

    cleaned = (reason or "").strip()
    if not cleaned:
        raise FxError(
            "A repricing needs a reason. The client is going to be asked to "
            "agree a different price and will want to know why."
        )
    if supplier_rate <= 0 or client_rate <= 0:
        raise FxError("A rate has to be a positive number.")
    if client_rate < supplier_rate:
        raise FxError(
            f"Quoting the client {client_rate} against a supplier rate of "
            f"{supplier_rate} would be selling at a loss."
        )

    was_supplier, was_client = order.supplier_rate, order.client_rate
    order.supplier_rate = supplier_rate
    order.client_rate = client_rate

    # A new price is a new offer. Their agreement to the old one does not
    # carry, and neither does the supplier's acceptance of an order priced
    # against it.
    order.client_confirmed_at = None
    order.supplier_confirmed_at = None
    _move(order, FxOrderStatus.RATE_QUOTED)

    await record_event(
        session, order, EventType.FX_ORDER_AMENDED, actor,
        kind="rate",
        reason=cleaned,
        was_supplier_rate=was_supplier,
        was_client_rate=was_client,
        supplier_rate=supplier_rate,
        client_rate=client_rate,
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
    cleaned = check_hash(tx_hash)
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


# --------------------------------------------------------------------------
# The outstanding book
#
# NexterPay's FX desk, through Jason on 2 October. The description of the job
# is worth keeping, because it says where the pain is and it is not where this
# module had assumed:
#
#     After that, he has to keep track of all the outstanding orders, keep
#     reauditing and following up, and updating his list, whilst dealing with
#     client chasers and supplier chasing.
#
# Everything before that sentence - asking for a rate, negotiating, quoting -
# he never calls painful. It is his craft. So the eleven steps above are not
# the problem; carrying the consequences of forty of them in your head is.
#
# `/npfx` already lists open deals, and it is not this. It answers "what is
# live", flat and in reference order. The book answers "what do I do next",
# which needs two things that list does not have: whose move it is as the
# organising fact, and how long it has been theirs. A list without ageing is
# the spreadsheet he is already updating by hand.
# --------------------------------------------------------------------------

# How long a deal may sit on one side before the book says so out loud.
#
# These were a guess until 3 October, when NexterPay's own supplier chat
# supplied the real number:
#
#     All Clients are now chasing these settlements as they have fallen
#     outside the 5 day limit
#
# So five days is not a threshold this project chose - it is the point at
# which NexterPay's clients start chasing them, which makes it the point at
# which the desk needs to have chased first. Overdue means late to somebody
# outside the building.
#
# Stale stays short of it on purpose: a deal that has gone quiet for three
# days is the one worth a nudge while there is still time to fix it. A book
# that only speaks once the client is already complaining has told the desk
# something they learned from the client.
STALE_AFTER = timedelta(days=3)
OVERDUE_AFTER = timedelta(days=5)


@dataclass(frozen=True)
class BookEntry:
    """One live deal, and how long it has been somebody's move.

    Holds the order rather than copying its figures out, so that nothing here
    has to decide which side's numbers it is carrying. The renderer asks for
    the codes it needs; the margin never passes through this object.
    """

    order: FxOrder
    waiting_since: datetime

    @property
    def waiting_on(self) -> str:
        """Whose move it is, as a chase list has to mean it.

        Usually the status knows. One case where it does not, found by running
        this against real deals on 3 October: `open_order` sets
        `RATE_REQUESTED` the moment a client asks about a rate, which is
        before anybody has chosen a supplier to ask. The status is honest in
        general - the next thing that happens is a supplier quoting us - but
        on a chase list it is a lie, because there is no supplier to chase.

        That deal is ours. Nobody outside this building is going to move it,
        and five days of it sitting under "waiting on suppliers" is five days
        of a deal nobody is working looking like a deal somebody owes us.
        """
        waiting = self.order.status.waiting_on
        if waiting == "Supplier" and self.order.supplier_code is None:
            return "NexterPay"
        return waiting

    @property
    def needs_a_supplier(self) -> bool:
        """Out for a quote with nobody asked. The reason it is ours."""
        return (
            self.order.status.waiting_on == "Supplier"
            and self.order.supplier_code is None
        )

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.waiting_since

    def is_stale(self, now: datetime | None = None) -> bool:
        return self.age(now) >= STALE_AFTER

    def is_overdue(self, now: datetime | None = None) -> bool:
        return self.age(now) >= OVERDUE_AFTER


async def _last_movement(
    session: AsyncSession, orders: list[FxOrder]
) -> dict[str, datetime]:
    """When each deal last did anything, keyed by its internal reference.

    Read from the event log rather than from a column on the order. Every
    transition in this module records an event carrying `fx_reference`, so the
    log already knows this and a `status_since` column would be a second,
    weaker record of the same fact - weaker because it would start life wrong
    for every deal already open on the day it shipped.

    The reference is matched in Python for the same reason `has_event` does
    it: `payload` is a generic JSON column and querying inside it is one of
    the few places SQLite and Postgres genuinely diverge, which is the
    divergence this project is least able to see.
    """
    item_ids = {order.client_work_item_id for order in orders}
    if not item_ids:
        return {}

    result = await session.execute(
        select(Event).where(Event.work_item_id.in_(item_ids))
    )

    latest: dict[str, datetime] = {}
    for event in result.scalars().all():
        reference = (event.payload or {}).get("fx_reference")
        if reference is None:
            continue
        when = as_utc(event.created_at)
        if when > latest.get(reference, when - timedelta(seconds=1)):
            latest[reference] = when
    return latest


async def outstanding_book(session: AsyncSession) -> list[BookEntry]:
    """Every deal still alive, oldest move first.

    Sorted by how long it has been waiting rather than by reference, because
    the order of this list is the order to work it in. A book sorted by
    reference is a book you have to read all of.
    """
    orders = await open_orders(session)
    if not orders:
        return []

    moved = await _last_movement(session, orders)

    item_ids = {order.client_work_item_id for order in orders}
    result = await session.execute(
        select(WorkItem).where(WorkItem.id.in_(item_ids))
    )
    raised = {item.id: as_utc(item.created_at) for item in result.scalars().all()}

    entries = [
        BookEntry(
            order=order,
            # A deal with no events at all has only just been opened, so the
            # request it hangs off is the honest answer. Falling back to "now"
            # would quietly reset the age of anything the log cannot explain.
            waiting_since=moved.get(
                order.display_reference,
                raised.get(order.client_work_item_id, utcnow()),
            ),
        )
        for order in orders
    ]
    entries.sort(key=lambda entry: entry.waiting_since)
    return entries
