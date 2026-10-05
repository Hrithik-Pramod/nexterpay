"""Moving an FX deal between NexterPay and the two counterparties.

The same safety rule as `app.services.relay`, and for higher stakes: nothing
reaches a counterparty unless somebody deliberately sends it, and here what is
sent is a price.

**There are exactly four functions in this module that write to a
counterparty chat**, and each one takes a side and composes through
`fx.view_for`, which reads that side's columns alone:

* `send_rate_quote` - the rate, with Yes / No
* `send_order` - the order, with a button to confirm it
* `send_settlement` - the hash, with a button to confirm receipt
* `notify_rejected` - telling a supplier their price was not taken

It was three until 16 September. `send_rate_quote` was deliberately not built:
quoting a client is a conversation, and the desk said it in their own words
using Reply to Client. NexterPay asked for it outright - "if we have the rates,
we should have option to send the client a message" - which is their call, and
the reason the decision was flagged rather than quietly made.

Nothing else here touches a counterparty group. A fifth would be a leak waiting
to happen, and `test_only_these_functions_may_write_to_a_counterparty` holds the
same list and fails if one appears. That test getting harder to satisfy is the
point of it; it should be argued with, not widened.

The internal commentary is separate and goes into the Operations topic, where
both rates may appear together because the whole topic is staff-only.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, FxOrder, Message, WorkItem
from app.domain import fx
from app.domain.enums import FxSide, MessageDirection
from app.domain.work_items import Actor
from app.services.gateway import TelegramGateway
from app.services.relay import _record_message, chats_for

logger = logging.getLogger(__name__)


async def _chat_for_side(
    session: AsyncSession, order: FxOrder, side: FxSide
) -> tuple[Chat, Chat]:
    """(the counterparty's group, our Operations Group) for one side.

    Resolved through the work item that side is conducted on, so an FX order
    cannot address a group that the underlying request does not already reach.
    Every existing guarantee about which group a request can write to therefore
    still applies here rather than being restated.
    """
    work_item_id = (
        order.client_work_item_id
        if side is FxSide.CLIENT
        else order.supplier_work_item_id
    )
    if work_item_id is None:
        raise fx.FxError(
            f"{order.display_reference} has no {side.value} request yet, so "
            f"there is nowhere to send that."
        )
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        raise fx.FxError(f"The {side.value} request for {order.display_reference} is gone.")
    return await chats_for(session, item)


def order_text(order: FxOrder, side: FxSide) -> str:
    """What one counterparty is shown. Composed through `fx.view_for`.

    The instruction at the end matters as much as the figures. An order that
    arrives without saying what to do with it gets replied to in free text,
    and a free-text "yes" is not a confirmation anybody can point at later.
    """
    view = fx.view_for(order, side)
    lines = view.lines()
    lines.append("")
    lines.append("Please confirm these figures are correct.")
    return "\n".join(lines)


def settlement_text(order: FxOrder) -> str:
    """The hash, and the link to look it up.

    NexterPay asked for the hash as a Tronscan link on 7 September. The raw
    hash stays on its own line as well - a link is convenient, but the hash is
    the thing a finance team pastes into their own records.
    """
    view = fx.view_for(order, FxSide.CLIENT)
    lines = [
        f"{view.reference} has been settled.",
        "",
        fx.format_money(view.receives) + f" {view.receives_currency or ''}".rstrip(),
    ]

    # A settlement recorded from a pasted block may not carry a hash yet -
    # their supplier sends the figures and the proof as two messages, and
    # `/nphash` exists to attach the second one later. The client was being
    # told "Transaction: None", followed by a bare Tronscan link to nothing.
    #
    # Saying nothing is better than saying None: the figure and the request to
    # confirm are the message, and the proof follows when it arrives.
    if (order.tx_hash or "").strip():
        lines += ["", f"Transaction: {order.tx_hash}"]
        link = fx.explorer_link(order.chain, order.tx_hash)
        if link:
            lines.append(link)
    lines.append("")
    lines.append("Please confirm once you have received it.")
    return "\n".join(lines)


def rate_quote_text(order: FxOrder) -> str:
    """The rate, in NexterPay's shape: "rate on INR is 89.50".

    Composed through `fx.view_for` like everything else that leaves here, so
    the supplier's rate is not reachable from this function even by mistake -
    it reads the client's columns and cannot see the other side's.

    The currency leads the sentence because without it the number is not a
    price. Every supplier quotes local currency per 1 USDT, so 89.50 is
    meaningless until it is 89.50 rupees.
    """
    view = fx.view_for(order, FxSide.CLIENT)
    where = f" on {view.currency_code}" if view.currency_code else ""
    return "\n".join([
        view.reference,
        "",
        f"Rate{where} is {fx.format_money(view.rate)}.",
        "",
        "Would you like to proceed?",
    ])


# --------------------------------------------------------------------------
# The four ways out
# --------------------------------------------------------------------------

async def rate_quote_already_sent(session: AsyncSession, order: FxOrder) -> bool:
    """Has this exact rate already gone to this client?

    Found live on 3 October. `tell_client_the_rate` had no guard and never
    cleared its button, so "✉ Send the rate to the client" stayed tappable for
    ever - and tapping it again sent the client a second copy of a price they
    had already agreed, carrying a second live pair of Yes and No buttons.

    This is the same fault NexterPay reported on 29 September as duplicate
    messages. That round fixed the two handlers where it had been noticed and
    not the third, which is a habit this project has: fixing a fault where it
    was seen rather than where it lives.

    Matched on the rendered text rather than on an event, because a requote is
    legitimate - a client turns a price down, the desk prices it again, and
    the same path sends it. A new price produces different text and goes
    through. A request to send a client a message identical to one they have
    already had is the thing being refused, and refusing that is correct even
    when it was deliberate.
    """
    result = await session.execute(
        select(Message).where(
            Message.work_item_id == order.client_work_item_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.text == rate_quote_text(order),
        )
    )
    return result.scalars().first() is not None


async def send_rate_quote(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    *,
    actor: Actor,
    keyboard=None,
) -> None:
    """The rate to the client, with Yes and No.

    Always the client. A supplier is never asked whether they would like to
    proceed with a rate - they gave us one.

    Recorded as an outbound message like any other, which matters more here
    than elsewhere: this is the moment a price is put in front of a client, and
    the record of exactly what was said is the thing a dispute comes back to.
    """
    counterparty, ops = await _chat_for_side(session, order, FxSide.CLIENT)
    text = rate_quote_text(order)

    sent = await gateway.send_message(
        counterparty.telegram_chat_id, text, reply_markup=keyboard
    )
    item = await session.get(WorkItem, order.client_work_item_id)
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=counterparty.telegram_chat_id,
        message_id=sent.message_id,
        sender_name=actor.name,
        text=text,
    )
    await _announce(session, gateway, order, ops, "Rate sent to the client.")


async def send_order(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    side: FxSide,
    *,
    actor: Actor,
    keyboard=None,
) -> None:
    """The order, to whichever side it belongs to.

    One function for both sides rather than two, deliberately. Two would mean
    two places composing figures, and the second one is where somebody
    eventually reads the wrong column. This one cannot: it is handed a side and
    passes it straight to `view_for`.
    """
    counterparty, ops = await _chat_for_side(session, order, side)
    text = order_text(order, side)

    sent = await gateway.send_message(
        counterparty.telegram_chat_id, text, reply_markup=keyboard
    )
    # Kept so the order can be withdrawn if its figures change before the
    # counterparty answers. See `withdraw_order`.
    if side is FxSide.CLIENT:
        order.client_order_message_id = sent.message_id
    else:
        order.supplier_order_message_id = sent.message_id
    work_item_id = (
        order.client_work_item_id
        if side is FxSide.CLIENT
        else order.supplier_work_item_id
    )
    item = await session.get(WorkItem, work_item_id)
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=counterparty.telegram_chat_id,
        message_id=sent.message_id,
        sender_name=actor.name,
        text=text,
    )
    await _announce(session, gateway, order, ops, f"Order sent to the {side.value}.")


async def send_settlement(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    *,
    actor: Actor,
    keyboard=None,
) -> None:
    """The hash, to the client. Always the client - a supplier told us."""
    counterparty, ops = await _chat_for_side(session, order, FxSide.CLIENT)
    text = settlement_text(order)

    sent = await gateway.send_message(
        counterparty.telegram_chat_id, text, reply_markup=keyboard
    )
    item = await session.get(WorkItem, order.client_work_item_id)
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=counterparty.telegram_chat_id,
        message_id=sent.message_id,
        sender_name=actor.name,
        text=text,
    )
    await _announce(session, gateway, order, ops, "Settlement passed to the client.")


async def notify_rejected(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    *,
    actor: Actor,
    reason: str,
) -> None:
    """Telling a supplier their price was not taken.

    Carries no figure at all - not theirs, and certainly not the one we went
    with. "We are not able to work with that rate on this one" is the whole
    message. A supplier who learns what beat them learns the market we buy in.
    """
    counterparty, ops = await _chat_for_side(session, order, FxSide.SUPPLIER)
    view = fx.view_for(order, FxSide.SUPPLIER)
    text = (
        f"{view.reference} — we are not able to work with that rate on this "
        f"one. Thank you for quoting."
    )
    await gateway.send_message(counterparty.telegram_chat_id, text)
    await _announce(
        session, gateway, order, ops,
        f"Told the supplier their rate was not taken ({reason}).",
    )


async def announce_counterparty_reply(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    side: FxSide,
    line: str,
) -> None:
    """A counterparty answered one of our buttons. Tell the desk.

    NexterPay, 29 September, after accepting a rate from the client group:
    "I didn't receive any sort of confirmation that client accepted the rate
    or so."

    Everything this module sent *outward* announced itself into the Operations
    topic - rate sent, order sent, settlement passed, rejection notified - and
    nothing announced what came *back*. The events were recorded, so the
    history was complete and `/npfx` could be asked; but a desk waiting on a
    client is not going to poll a command, and the whole point of the topic is
    that the conversation appears in it.

    Internal only, like every other line here, so it may name both sides.
    """
    _, ops = await _chat_for_side(session, order, side)
    await _announce(session, gateway, order, ops, line)


# --------------------------------------------------------------------------
# Internal commentary
# --------------------------------------------------------------------------

async def _announce(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    ops: Chat,
    line: str,
) -> None:
    """Into the Operations topic. Staff-only, so it may name both sides."""
    item = await session.get(WorkItem, order.client_work_item_id)
    thread_id = item.topic_id if item else None
    if thread_id is None:
        logger.debug("No topic for %s; skipping announcement", order.display_reference)
        return
    await gateway.send_message(
        ops.telegram_chat_id, f"• {line}", thread_id=thread_id
    )


def desk_summary(order: FxOrder) -> str:
    """Both halves of the deal, for the Operations topic only.

    The one place the two rates sit together, and therefore the one place the
    margin is visible. Never composed for a counterparty - `order_text` is what
    goes outward, and it takes a side.
    """
    lines = [
        f"{order.display_reference} — {order.status.label}",
        f"Waiting on: {order.status.waiting_on}",
        "",
        "Client",
        f"  rate {fx.format_money(order.client_rate)}"
        f"  pays {fx.format_money(order.client_pays)} {order.client_pays_currency or ''}"
        f"  receives {fx.format_money(order.client_receives)} "
        f"{order.client_receives_currency or ''}",
        "",
        "Supplier",
        f"  rate {fx.format_money(order.supplier_rate)}"
        f"  pays {fx.format_money(order.supplier_pays)} {order.supplier_pays_currency or ''}"
        f"  receives {fx.format_money(order.supplier_receives)} "
        f"{order.supplier_receives_currency or ''}",
    ]
    if order.margin is not None:
        lines += ["", f"Margin  {fx.format_money(order.margin)}"]
    if order.tx_hash:
        lines += ["", f"Hash  {order.tx_hash}"]
    return "\n".join(line.rstrip() for line in lines)


WITHDRAWN_ORDER_TEXT = (
    "This order has been replaced — please ignore the figures above. "
    "An updated one will follow."
)


async def withdraw_order(
    session: AsyncSession,
    gateway: TelegramGateway,
    order: FxOrder,
    side: FxSide,
) -> bool:
    """Take back an order a counterparty has not answered yet.

    Found live on 4 October. A deal was amended from 250,000 to 200,000, and
    the order message sitting in the client's group kept both its old figures
    and its live Confirm button. Tapping it recorded the client as having
    confirmed 250,000 - a number that was no longer the order and that they
    had agreed in good faith from what was in front of them.

    A client agreeing to a figure they were never shown is the worst thing
    this platform can do that is not a margin leak, and it was reachable by
    one tap on a message nobody had thought to take down.

    So the button goes first and the text second. If only the first succeeds
    the order cannot be confirmed, which is the half that matters; if the text
    edit also lands, the stale figures stop being readable as current. Both
    are attempted and neither is allowed to raise - a withdrawal that fails
    must not take the amendment down with it, because an amended deal with a
    stale message is recoverable and an unrecorded amendment is not.

    Returns whether the button was successfully removed, so the caller can
    tell the desk to go and say something if it was not.
    """
    message_id = (
        order.client_order_message_id
        if side is FxSide.CLIENT
        else order.supplier_order_message_id
    )
    if message_id is None:
        return True  # nothing outstanding to take back

    counterparty, _ = await _chat_for_side(session, order, side)
    cleared = False

    try:
        await gateway.edit_reply_markup(
            counterparty.telegram_chat_id, message_id, reply_markup=None
        )
        cleared = True
    except Exception:
        logger.exception(
            "Could not clear the order buttons for %s", order.display_reference
        )

    try:
        await gateway.edit_message_text(
            counterparty.telegram_chat_id, message_id, WITHDRAWN_ORDER_TEXT
        )
    except Exception:
        logger.debug(
            "Could not rewrite the withdrawn order text for %s",
            order.display_reference, exc_info=True,
        )

    if side is FxSide.CLIENT:
        order.client_order_message_id = None
    else:
        order.supplier_order_message_id = None
    await session.flush()
    return cleared
