"""Moving an FX deal between NexterPay and the two counterparties.

The same safety rule as `app.services.relay`, and for higher stakes: nothing
reaches a counterparty unless somebody deliberately sends it, and here what is
sent is a price.

**There are exactly three functions in this module that write to a
counterparty chat**, and each one takes a side and composes through
`fx.view_for`, which reads that side's columns alone:

* `send_order` - the order, with a button to confirm it
* `send_settlement` - the hash, with a button to confirm receipt
* `notify_rejected` - telling a supplier their price was not taken

Nothing else here touches a counterparty group. A fourth would be a leak
waiting to happen, and `test_only_these_functions_may_write_to_a_counterparty`
holds the same list and fails if one appears.

The internal commentary is separate and goes into the Operations topic, where
both rates may appear together because the whole topic is staff-only.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, FxOrder, WorkItem
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
        "",
        f"Transaction: {order.tx_hash}",
    ]
    link = fx.explorer_link(order.chain, order.tx_hash or "")
    if link:
        lines.append(link)
    lines.append("")
    lines.append("Please confirm once you have received it.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The three ways out
# --------------------------------------------------------------------------

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
