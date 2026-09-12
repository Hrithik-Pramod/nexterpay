"""The FX commands, in the Operations Group.

`/npordercl` and `/npordersu` create the two halves of a deal. Two commands
rather than one with a picker, for the reason NexterPay gave on 12 September:
which side you are creating decides which figures you are about to type and
which group they are about to reach, so the intent belongs in the command.

Everything here runs on our side. Neither counterparty types anything - they
receive an order and tap Confirm. The client never learns the supplier's rate
and the supplier never learns the client's name, and both of those facts are
properties of `fx_relay`, not of care taken in this file.

The three decisions that a person can get wrong - is this a number, do the
figures agree, what will each side be shown - are pulled out as functions so
they can be tested. An FSM handler cannot be called from a test, and that gap
is where this project's last three bugs lived.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot import commands as cmd
from app.bot.deps import explain, gateway, prompt_for, refusal_reason, staff_context
from app.db.base import session_scope
from app.db.models import Chat, Client, FxOrder, WorkItem
from app.domain import fx
from app.domain.enums import ChatKind, FxOrderStatus, FxSide
from app.domain.work_items import Actor
from app.services import fx_relay

logger = logging.getLogger(__name__)
router = Router(name="fx")


class FxCompose(StatesGroup):
    awaiting_amount = State()
    awaiting_rate = State()
    awaiting_receives = State()
    awaiting_name = State()


# --------------------------------------------------------------------------
# The decisions, where a test can reach them
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Figures:
    """What somebody typed, once it is known to be arithmetic."""

    pays: Decimal
    pays_currency: str
    rate: Decimal
    receives: Decimal
    receives_currency: str


def parse_pair(text: str) -> tuple[Decimal, str]:
    """`250000 EUR` - an amount and the currency it is in.

    Both together because an amount without a currency is not an amount. The
    figure is going to a counterparty as a commitment, and "250,000" on its own
    has been wrong at least once in the history of every payments business.
    """
    parts = (text or "").strip().split()
    if len(parts) < 2:
        raise fx.FxError(
            "Give me the amount and the currency, like “250000 EUR”."
        )
    amount = fx.parse_amount(parts[0])
    currency = parts[1].strip().upper()
    if not currency.isalpha() or not 2 <= len(currency) <= 8:
        raise fx.FxError(f"“{parts[1]}” does not look like a currency.")
    return amount, currency


def check_consistent(figures: Figures) -> str | None:
    """Do the three numbers agree with each other?

    Returns a warning rather than raising. Rates in practice carry fees,
    rounding and spreads that a bare multiplication will not reproduce, so
    refusing a deal because the arithmetic is off by a little would block real
    business. But an order that is out by a factor of ten is a typo, and the
    person typing it is the last one who can catch it cheaply.
    """
    if figures.rate == 0:
        return None
    expected = figures.pays * figures.rate
    if expected == 0:
        return None
    drift = abs(figures.receives - expected) / expected
    if drift > Decimal("0.02"):
        return (
            f"Check these: {fx.format_money(figures.pays)} at "
            f"{fx.format_money(figures.rate)} comes to about "
            f"{fx.format_money(expected)}, not "
            f"{fx.format_money(figures.receives)}."
        )
    return None


def default_account_name(side: FxSide, client: Client, supplier: Client | None) -> str:
    """What goes on the order as the account.

    NexterPay, 12 September: "for Client it is simple its their business name,
    but for supplier, we have coding Nexterpay7 etc". Offered as a default and
    editable, because the supplier code differs per supplier and only the desk
    knows which one.
    """
    if side is FxSide.CLIENT:
        return client.name
    return "Nexterpay"


def confirm_keyboard(order_id: int, side: FxSide) -> InlineKeyboardMarkup:
    """What the counterparty taps.

    One button. A Reject would look symmetrical and be wrong: a counterparty
    who does not accept these figures has something to say about why, and that
    is a conversation rather than a button. Rejection is recorded by the desk,
    from what they actually said.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Confirm these figures",
            callback_data=f"fx:confirm:{order_id}:{side.value}",
        )
    ]])


def receipt_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Confirm receipt", callback_data=f"fx:receipt:{order_id}"
        )
    ]])


def preview_text(side: FxSide, target: str, figures: Figures, account: str) -> str:
    """What the desk sees before anything is sent.

    Shows the same figures the counterparty will see and says who they are
    going to. This screen exists to stop somebody sending a client's numbers to
    a supplier, so it names both.
    """
    return "\n".join([
        f"This will be sent to {target} as the {side.value} side:",
        "",
        f"Account: {account}",
        f"Rate: {fx.format_money(figures.rate)}",
        f"They send: {fx.format_money(figures.pays)} {figures.pays_currency}",
        f"They receive: {fx.format_money(figures.receives)} {figures.receives_currency}",
        "",
        "Nothing has been sent yet.",
    ])


def _send_keyboard(order_id: int, side: FxSide) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✉ Send to the {side.value}",
            callback_data=f"fx:send:{order_id}:{side.value}",
        )],
        [InlineKeyboardButton(text="Cancel", callback_data=f"fx:cancel:{order_id}")],
    ])


# --------------------------------------------------------------------------
# Picking the deal
# --------------------------------------------------------------------------

async def _open_deals(session) -> list[FxOrder]:
    result = await session.execute(
        select(FxOrder)
        .where(FxOrder.status != FxOrderStatus.CLOSED)
        .order_by(FxOrder.reference)
    )
    return list(result.scalars().all())


def _deal_keyboard(orders: list[FxOrder], side: FxSide) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{o.display_reference} · {o.status.label}"[:60],
            callback_data=f"fx:pick:{o.id}:{side.value}",
        )]
        for o in orders
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="fx:cancel:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(cmd.any_case(cmd.ORDER_CLIENT))
async def order_client(message: Message, state: FSMContext) -> None:
    """`/npordercl` - create the client's half of a deal."""
    await _start(message, state, FxSide.CLIENT)


@router.message(cmd.any_case(cmd.ORDER_SUPPLIER))
async def order_supplier(message: Message, state: FSMContext) -> None:
    """`/npordersu` - create the supplier's half."""
    await _start(message, state, FxSide.SUPPLIER)


async def _start(message: Message, state: FSMContext, side: FxSide) -> None:
    user = message.from_user
    async with session_scope() as session:
        ctx = await staff_context(session, message.chat.id, user.id if user else None)
        if ctx is None:
            await message.reply(
                await refusal_reason(
                    user.id if user else None, session, message.chat.id
                )
            )
            return
        deals = await _open_deals(session)
        markup = _deal_keyboard(deals, side) if deals else None

    if not deals:
        await message.reply(
            "There are no open FX deals. A deal starts from the client's "
            "request - open it and use More → Start FX."
        )
        return

    await state.clear()
    await message.reply(
        f"Which deal is this {side.value} order for?", reply_markup=markup
    )


@router.callback_query(F.data.startswith("fx:pick:"))
async def pick_deal(query: CallbackQuery, state: FSMContext) -> None:
    _, _, order_id, side_value = (query.data or "").split(":")
    side = FxSide(side_value)

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        order = await session.get(FxOrder, int(order_id))
        if order is None:
            await query.answer("That deal no longer exists.", show_alert=True)
            return
        reference = order.display_reference

    await state.set_state(FxCompose.awaiting_amount)
    await state.update_data(order_id=int(order_id), side=side.value)
    text, markup, mode = prompt_for(
        query.from_user,
        f"{reference} — what does the {side.value} send? Amount and currency, "
        f"like “250000 EUR”.",
        placeholder="Amount and currency",
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)
    await query.answer()


# --------------------------------------------------------------------------
# The three figures, one at a time
# --------------------------------------------------------------------------

@router.message(FxCompose.awaiting_amount)
async def capture_amount(message: Message, state: FSMContext) -> None:
    try:
        amount, currency = parse_pair(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    await state.update_data(pays=str(amount), pays_currency=currency)
    await state.set_state(FxCompose.awaiting_rate)
    text, markup, mode = prompt_for(
        message.from_user, "And the rate?", placeholder="Rate",
    )
    await message.answer(text, reply_markup=markup, parse_mode=mode)


@router.message(FxCompose.awaiting_rate)
async def capture_rate(message: Message, state: FSMContext) -> None:
    try:
        rate = fx.parse_rate(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    await state.update_data(rate=str(rate))
    await state.set_state(FxCompose.awaiting_receives)
    text, markup, mode = prompt_for(
        message.from_user,
        "And what do they receive? Amount and currency.",
        placeholder="Amount and currency",
    )
    await message.answer(text, reply_markup=markup, parse_mode=mode)


@router.message(FxCompose.awaiting_receives)
async def capture_receives(message: Message, state: FSMContext) -> None:
    try:
        amount, currency = parse_pair(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    data = await state.get_data()
    figures = Figures(
        pays=Decimal(data["pays"]),
        pays_currency=data["pays_currency"],
        rate=Decimal(data["rate"]),
        receives=amount,
        receives_currency=currency,
    )
    warning = check_consistent(figures)

    await state.update_data(receives=str(amount), receives_currency=currency)
    await state.set_state(FxCompose.awaiting_name)

    side = FxSide(data["side"])
    async with session_scope() as session:
        order = await session.get(FxOrder, data["order_id"])
        client = await session.get(Client, order.client_id) if order else None
        supplier = (
            await session.get(Client, order.supplier_id)
            if order and order.supplier_id else None
        )
        suggested = default_account_name(side, client, supplier) if client else "—"

    if warning:
        # Said before the name is asked for, so it is read rather than skipped
        # past on the way to the send button.
        await message.answer(f"⚠ {warning}\n\nCarry on if that is right.")

    text, markup, mode = prompt_for(
        message.from_user,
        f"And the name on the order? Suggested: {suggested}",
        placeholder=suggested,
    )
    await message.answer(text, reply_markup=markup, parse_mode=mode)


@router.message(FxCompose.awaiting_name)
async def capture_name(message: Message, state: FSMContext) -> None:
    account = (message.text or "").strip()
    if not account:
        await message.reply("The order needs a name. It is what they will see.")
        return

    data = await state.get_data()
    side = FxSide(data["side"])
    figures = Figures(
        pays=Decimal(data["pays"]),
        pays_currency=data["pays_currency"],
        rate=Decimal(data["rate"]),
        receives=Decimal(data["receives"]),
        receives_currency=data["receives_currency"],
    )

    async with session_scope() as session:
        order = await session.get(FxOrder, data["order_id"])
        if order is None:
            await state.clear()
            await message.reply("That deal no longer exists.")
            return
        target = await _target_name(session, order, side)

    await state.update_data(account=account)
    await message.answer(
        preview_text(side, target, figures, account),
        reply_markup=_send_keyboard(data["order_id"], side),
    )


async def _target_name(session, order: FxOrder, side: FxSide) -> str:
    counterparty_id = order.client_id if side is FxSide.CLIENT else order.supplier_id
    if counterparty_id is None:
        return "nobody yet"
    counterparty = await session.get(Client, counterparty_id)
    return counterparty.name if counterparty else "unknown"


# --------------------------------------------------------------------------
# Sending, and the counterparty confirming
# --------------------------------------------------------------------------

@router.callback_query(F.data.startswith("fx:send:"))
async def send_order(query: CallbackQuery, state: FSMContext) -> None:
    _, _, order_id, side_value = (query.data or "").split(":")
    side = FxSide(side_value)
    data = await state.get_data()
    await query.answer()

    if data.get("order_id") != int(order_id) or "account" not in data:
        await state.clear()
        await query.message.answer("That draft has already been sent, or it expired.")
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            return
        _, actor = ctx
        order = await session.get(FxOrder, int(order_id))
        if order is None:
            await state.clear()
            await query.message.answer("That deal no longer exists.")
            return

        create = (
            fx.create_client_order if side is FxSide.CLIENT else fx.create_supplier_order
        )
        try:
            await create(
                session, order,
                account_name=data["account"],
                rate=Decimal(data["rate"]),
                pays=Decimal(data["pays"]),
                pays_currency=data["pays_currency"],
                receives=Decimal(data["receives"]),
                receives_currency=data["receives_currency"],
                actor=actor,
            )
            await fx_relay.send_order(
                session, gateway(), order, side, actor=actor,
                keyboard=confirm_keyboard(order.id, side),
            )
        except Exception as exc:
            logger.exception("FX order send failed for %s", order_id)
            await query.message.answer(explain(exc))
            return

    await state.clear()
    await query.message.answer(f"Sent to the {side.value}.")


@router.callback_query(F.data.startswith("fx:confirm:"))
async def counterparty_confirms(query: CallbackQuery) -> None:
    """Tapped in the counterparty's own group, by the counterparty.

    No staff check: the whole point is that this is them, not us. What is
    checked is that the button was tapped in the group the order was sent to -
    a callback carries whatever id it was built with, and one confirmed from
    the wrong room is not a confirmation.

    Every branch answers. A counterparty who taps Confirm and gets silence
    concludes the deal is agreed, which on an FX order is the most expensive
    wrong conclusion available - so there is no path through this that says
    nothing, including the ones that should never be reached.

    Answered after the lookup rather than before it, unlike the staff buttons.
    Those do real work and needed the spinner stopped first; this is two
    lookups by primary key, and answering at the end means the toast can carry
    the actual outcome rather than a blank acknowledgement.
    """
    _, _, order_id, side_value = (query.data or "").split(":")
    side = FxSide(side_value)

    async with session_scope() as session:
        order = await session.get(FxOrder, int(order_id))
        if order is None:
            await query.answer(
                "That order no longer exists. Please speak to us before "
                "acting on it.",
                show_alert=True,
            )
            return
        expected = await _group_for_side(session, order, side)
        if expected is None or query.message.chat.id != expected:
            logger.info(
                "FX confirm from the wrong chat: order=%s chat=%s",
                order_id, query.message.chat.id,
            )
            await query.answer(
                "This order can only be confirmed in the group it was sent to.",
                show_alert=True,
            )
            return

        who = query.from_user.full_name if query.from_user else "Counterparty"
        actor = Actor(name=who, telegram_user_id=
                      query.from_user.id if query.from_user else None)
        try:
            if side is FxSide.CLIENT:
                await fx.client_confirms(session, order, actor=actor)
            else:
                await fx.supplier_accepts(session, order, actor=actor)
        except fx.FxError:
            # Already confirmed, most likely. Saying so is kinder than silence
            # and safer than confirming twice.
            await query.answer("That has already been confirmed, thank you.")
            return

    await query.answer("Confirmed.")
    await query.message.answer("Thank you — confirmed.")


@router.callback_query(F.data.startswith("fx:receipt:"))
async def client_confirms_receipt(query: CallbackQuery) -> None:
    """The client saying the funds have arrived, which is what closes a deal.

    Speaks in every branch, for the same reason as confirming an order: a
    client who taps and hears nothing assumes we have been told, and stops
    chasing something that is still open on our side.
    """
    order_id = int((query.data or "").split(":")[2])

    async with session_scope() as session:
        order = await session.get(FxOrder, order_id)
        if order is None:
            await query.answer(
                "That order no longer exists. Please speak to us.", show_alert=True
            )
            return
        expected = await _group_for_side(session, order, FxSide.CLIENT)
        if expected is None or query.message.chat.id != expected:
            logger.info(
                "FX receipt from the wrong chat: order=%s chat=%s",
                order_id, query.message.chat.id,
            )
            await query.answer(
                "This can only be confirmed in the group it was sent to.",
                show_alert=True,
            )
            return

        who = query.from_user.full_name if query.from_user else "Client"
        actor = Actor(name=who, telegram_user_id=
                      query.from_user.id if query.from_user else None)
        try:
            await fx.client_confirms_receipt(session, order, actor=actor)
        except fx.FxError:
            await query.answer(
                "That has already been confirmed, thank you.", show_alert=True
            )
            return

    await query.answer("Confirmed.")
    await query.message.answer("Thank you — this order is now closed.")


@router.callback_query(F.data.startswith("fx:cancel:"))
async def cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await query.message.answer("Cancelled. Nothing was sent.")


async def _group_for_side(session, order: FxOrder, side: FxSide) -> int | None:
    work_item_id = (
        order.client_work_item_id if side is FxSide.CLIENT
        else order.supplier_work_item_id
    )
    if work_item_id is None:
        return None
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        return None
    chat = await session.get(Chat, item.source_chat_id)
    return chat.telegram_chat_id if chat else None


# --------------------------------------------------------------------------
# Looking at the book
# --------------------------------------------------------------------------

@router.message(cmd.any_case(cmd.FX_DEALS))
async def list_deals(message: Message) -> None:
    """`/npfx` - every open deal and whose move it is.

    The Operations Group only. The desk summary carries both rates, which is
    the margin, so this is refused anywhere else rather than trimmed.
    """
    user = message.from_user
    async with session_scope() as session:
        ctx = await staff_context(session, message.chat.id, user.id if user else None)
        if ctx is None:
            await message.reply(
                await refusal_reason(
                    user.id if user else None, session, message.chat.id
                )
            )
            return
        deals = await _open_deals(session)
        lines = [
            f"{o.display_reference} · {o.status.label} · waiting on "
            f"{o.status.waiting_on}"
            for o in deals
        ]

    if not lines:
        await message.reply("No open FX deals.")
        return
    await message.reply("Open FX deals:\n\n" + "\n".join(lines))


async def start_deal(session, item: WorkItem, actor: Actor) -> FxOrder:
    """Open a deal against a client request. Used by the More menu.

    Kept here rather than in the button handler so it can be called from a
    test, which is the whole reason the last three bugs in this project were
    invisible to 500 passing tests.
    """
    chat = await session.get(Chat, item.source_chat_id)
    if chat is None or chat.kind is not ChatKind.CLIENT or chat.is_supplier:
        raise fx.FxError(
            "An FX deal starts from a client's request. Open it in the "
            "client's topic rather than the supplier's."
        )
    client = await session.get(Client, item.client_id)
    return await fx.open_order(
        session, client=client, client_work_item=item, actor=actor
    )


__all__ = [
    "Figures",
    "check_consistent",
    "confirm_keyboard",
    "default_account_name",
    "parse_pair",
    "preview_text",
    "receipt_keyboard",
    "router",
    "start_deal",
]
