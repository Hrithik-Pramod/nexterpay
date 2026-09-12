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
from app.domain.enums import ChatKind, FxOrderStatus, FxSide, WorkItemStatus
from app.domain.work_items import Actor
from app.services import fx_relay

logger = logging.getLogger(__name__)
router = Router(name="fx")


class FxCompose(StatesGroup):
    awaiting_amount = State()
    awaiting_rate = State()
    awaiting_receives = State()
    awaiting_name = State()


class FxQuote(StatesGroup):
    """Recording what the supplier quoted us, and what we are quoting the client.

    Two rates in one flow rather than two commands, because they are decided
    together: our price is the supplier's plus the margin, and a desk that has
    just been given one is thinking about the other. Splitting them would also
    leave a deal sitting with a supplier rate and no client rate, which is a
    state nobody can act on and everybody would have to explain.
    """

    awaiting_supplier_rate = State()
    awaiting_client_rate = State()


class FxHash(StatesGroup):
    awaiting_hash = State()


class FxReject(StatesGroup):
    awaiting_reason = State()


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


# Rates carry fees, rounding and spreads that a bare multiplication will not
# reproduce, so the check has to allow some daylight. Two per cent is wide
# enough for real pricing and nowhere near wide enough to hide a typo.
CONSISTENCY_TOLERANCE = Decimal("0.02")


def _agrees(actual: Decimal, expected: Decimal) -> bool:
    if expected == 0:
        return actual == 0
    return abs(actual - expected) / expected <= CONSISTENCY_TOLERANCE


def check_consistent(figures: Figures) -> str | None:
    """Do the three numbers agree with each other, either way round?

    Both orientations are accepted, and that is the whole point of this
    function's shape. A rate is quoted against one of the two currencies, and
    which side of the deal that currency sits on **swaps between the client
    and the supplier**: the client sends 250,000 EUR and receives 291,050 USDT
    at 1.1642, while the supplier sends 290,000 USDT and receives 250,000 EUR
    at 1.16. Multiply what each of them sends by their rate and the client's
    figures agree while the supplier's are out by a third.

    This was live for one evening testing only the client's orientation, so it
    warned on every correct supplier order - found by putting a real deal
    through rather than by any test, because the tests only ever fed it a
    client's figures. A warning that fires every time is worse than no warning
    at all: people learn to click past it, and the one that matters is the
    tenfold typo on the client's side.

    Accepting both is also the honest thing to do while the rate convention is
    still an open question with NexterPay. Committing this check to one
    orientation would be asserting an answer nobody has given yet, and a
    genuine typo misses both readings anyway.

    Returns a warning rather than raising. An operator who knows the deal is
    right must be able to carry on.
    """
    if figures.rate == 0:
        return None

    # The client's shape: what they send, at the rate, is what they receive.
    forward = figures.pays * figures.rate
    # The supplier's: what they receive, at the rate, is what they send.
    reverse = figures.receives * figures.rate

    if _agrees(figures.receives, forward) or _agrees(figures.pays, reverse):
        return None

    # Neither reading works, so this is arithmetic rather than convention.
    # Reported in the forward direction because that is the one somebody
    # typing a client order is holding in their head.
    return (
        f"Check these: {fx.format_money(figures.pays)} at "
        f"{fx.format_money(figures.rate)} comes to about "
        f"{fx.format_money(forward)}, not "
        f"{fx.format_money(figures.receives)}."
    )


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
# The rate decisions
#
# Steps 3 and 4 of the route, which had no door at all until now. The order
# commands were built first and each of them refuses a deal that has not been
# quoted, so the flow ended one step after it began: the state machine was
# right and there was no way to satisfy it. Pulled out here for the same reason
# as everything else in this section - a test can call a function and cannot
# call an FSM handler.
# --------------------------------------------------------------------------

# Where a rate can still be entered. Not simply "not closed": once a client has
# confirmed figures, the price is fixed, and re-quoting from there would change
# a deal somebody has already agreed to.
QUOTABLE = (
    FxOrderStatus.RATE_REQUESTED,
    FxOrderStatus.RATE_QUOTED,
    FxOrderStatus.RATE_REJECTED,
)

# A margin wider than this is reported to the desk before anything is saved.
# Not refused - NexterPay set the prices, not this bot - but 1.1642 typed as
# 11.642 is a tenfold error that looks exactly like a good day until somebody
# sends it to a client, and a decimal point is the easiest key on the board to
# miss.
MARGIN_WARNING = Decimal("0.10")


def check_margin(supplier_rate: Decimal | None, client_rate: Decimal) -> str | None:
    """Does the pair of rates look like a price, or like a typo?

    Returns a warning, never an exception. `fx.quote_client` already refuses a
    client rate below the supplier's, which is the case that loses money; this
    catches the opposite mistake, which loses a client.
    """
    if supplier_rate is None or supplier_rate <= 0:
        return None
    if client_rate < supplier_rate:
        return (
            f"That is below the supplier's {fx.format_money(supplier_rate)}, so "
            f"the deal would lose money."
        )
    spread = (client_rate - supplier_rate) / supplier_rate
    if spread > MARGIN_WARNING:
        percent = (spread * 100).quantize(Decimal("0.1"))
        return (
            f"That is {percent}% over the supplier's "
            f"{fx.format_money(supplier_rate)}. Check the decimal point."
        )
    return None


def quote_preview(
    reference: str,
    supplier_name: str,
    supplier_rate: Decimal | None,
    client_rate: Decimal,
) -> str:
    """What the desk reads before the two rates are saved.

    Shows both, and the margin between them, which is the one number on this
    platform that must never leave the Operations Group. It is safe here
    because this message is composed for a staff topic and is never passed to
    `fx_relay` - the three functions that write to a counterparty all compose
    through `fx.view_for`, and none of them can reach this text.
    """
    lines = [f"{reference} — rates, not yet saved", ""]
    if supplier_rate is None:
        # Revising our own price on a deal already quoted. The supplier's rate
        # is on the deal but was not entered in this flow, and showing a figure
        # this screen did not collect would invite somebody to correct it here.
        lines.append(f"We quote the client: {fx.format_money(client_rate)}")
    else:
        lines += [
            f"{supplier_name} quoted us: {fx.format_money(supplier_rate)}",
            f"We quote the client:   {fx.format_money(client_rate)}",
            f"Margin:                {fx.format_money(client_rate - supplier_rate)}",
        ]
    lines += [
        "",
        "Saving this records the rate and moves the deal to Rate Quoted.",
        "Nothing is sent to either party.",
    ]
    return "\n".join(lines)


def rejectable_sides(order: FxOrder) -> list[FxSide]:
    """Which rejection is available on a deal, given where it is.

    Both exist - NexterPay described them as separate return paths on
    12 September - but never at the same moment. A supplier's rate can be
    turned down while we are still shopping for a price; ours can be turned
    down only once the client has been given it. Offering both always would
    mean offering one that the domain will refuse, which is a button that
    exists to produce an error message.
    """
    sides: list[FxSide] = []
    if (
        order.status in (FxOrderStatus.RATE_REQUESTED, FxOrderStatus.RATE_REJECTED)
        and order.supplier_rate is not None
    ):
        sides.append(FxSide.SUPPLIER)
    if order.status in (
        FxOrderStatus.RATE_QUOTED,
        FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
    ):
        sides.append(FxSide.CLIENT)
    return sides


def reject_prompt(side: FxSide) -> str:
    """Two different questions, because they lead to two different next moves.

    A supplier's price being no good is our judgement and the reason is ours.
    A client turning our price down is their words, and "too high" and "we have
    a better price elsewhere" are not the same conversation afterwards - which
    is why `fx.reject_client_rate` keeps the reason verbatim.
    """
    if side is FxSide.SUPPLIER:
        return (
            "Why is that rate no good? This is recorded against the deal. The "
            "supplier is told only that we cannot work with it — never the "
            "reason, and never the rate we went with instead."
        )
    return (
        "What did the client say? Their own words are worth more than a "
        "summary here — “too high” and “we have a better price elsewhere” "
        "lead to different conversations."
    )


# --------------------------------------------------------------------------
# Picking the deal
# --------------------------------------------------------------------------

async def _open_deals(
    session, *, allowed: tuple[FxOrderStatus, ...] | None = None
) -> list[FxOrder]:
    """Every live deal, or only those a particular step can act on.

    Filtering in the query rather than listing everything and refusing later.
    A picker that offers a deal and then says no is how somebody learns the
    state machine by trial and error in front of a client.
    """
    query = select(FxOrder).where(FxOrder.status != FxOrderStatus.CLOSED)
    if allowed is not None:
        query = query.where(FxOrder.status.in_(allowed))
    result = await session.execute(query.order_by(FxOrder.reference))
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
# `/npquote` - steps 3 and 4
# --------------------------------------------------------------------------

def _pick_keyboard(orders: list[FxOrder], verb: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{o.display_reference} · {o.status.label}"[:60],
            callback_data=f"fx:{verb}:{o.id}",
        )]
        for o in orders
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="fx:cancel:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _action_keyboard(label: str, data: str) -> InlineKeyboardMarkup:
    """One thing to do, and a way out of doing it.

    A function rather than four copies of the same two rows, and not for
    tidiness: a keyboard built inline inside a handler is invisible to the test
    that checks every button reaches a handler, because that test can only
    inspect keyboards it can construct. Two of this project's bugs were dead
    or missing buttons, and both were in code no test could call.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data)],
        [InlineKeyboardButton(text="Cancel", callback_data="fx:cancel:0")],
    ])


def _supplier_keyboard(order_id: int, requests) -> InlineKeyboardMarkup:
    """Which supplier request this deal is priced against.

    `requests` is a list of (work item, supplier) as `_supplier_requests`
    returns it. The subject is on the button because a supplier with three open
    requests is ordinary, and the reference alone does not say which of them
    asked for a price.
    """
    rows = [
        [InlineKeyboardButton(
            text=f"{item.display_reference} · {client.name} · {item.subject}"[:60],
            callback_data=f"fx:qsup:{order_id}:{item.id}",
        )]
        for item, client in requests
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="fx:cancel:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _side_keyboard(order_id: int, sides: list[FxSide]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=(
                "The supplier's rate is no good" if side is FxSide.SUPPLIER
                else "The client turned our rate down"
            ),
            callback_data=f"fx:rside:{order_id}:{side.value}",
        )]
        for side in sides
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="fx:cancel:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _supplier_requests(session, department) -> list[tuple[WorkItem, Client]]:
    """Open requests sitting in supplier groups for this department.

    This is how a deal learns which supplier it belongs to, and it is
    deliberately not a list of suppliers. The supplier half of an FX deal is
    conducted on a real request in their group - the one raised with
    `/npnewsu` asking for a price - and pointing the deal at that request is
    what later lets the order reach them at all: `fx_relay` resolves the
    group through the work item, so a deal with no supplier request has
    nowhere to send anything.

    Scoped to the department for the same reason `/npnewsu` is: a Finance desk
    has no business pricing a deal against a Support supplier's request.
    """
    query = (
        select(WorkItem, Client)
        .join(Chat, WorkItem.source_chat_id == Chat.id)
        .join(Client, WorkItem.client_id == Client.id)
        .where(
            Chat.is_supplier.is_(True),
            Chat.is_active.is_(True),
            WorkItem.department == department,
            WorkItem.status.notin_(
                [WorkItemStatus.COMPLETED, WorkItemStatus.CLOSED]
            ),
        )
        .order_by(WorkItem.reference.desc())
        .limit(30)
    )
    result = await session.execute(query)
    return [(item, client) for item, client in result.all()]


@router.message(cmd.any_case(cmd.QUOTE))
async def quote(message: Message, state: FSMContext) -> None:
    """`/npquote` - what the supplier quoted us, and what we quote the client.

    The step that was missing. Both order commands refuse a deal that has not
    been quoted, and nothing could quote one, so every deal stopped at Rate
    Requested with a correct refusal and no way past it.
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
        deals = await _open_deals(session, allowed=QUOTABLE)
        markup = _pick_keyboard(deals, "qdeal") if deals else None

    if not deals:
        await message.reply(
            "No deal is waiting on a rate. A deal starts from the client's "
            "request — open it in this group and use More → Start FX deal."
        )
        return

    await state.clear()
    await message.reply("Which deal are you pricing?", reply_markup=markup)


@router.callback_query(F.data.startswith("fx:qdeal:"))
async def quote_pick_deal(query: CallbackQuery, state: FSMContext) -> None:
    """Where the flow forks.

    A deal that has never been priced needs a supplier before it needs a rate,
    because the supplier's request is where the order will eventually be sent.
    A deal already at Rate Quoted has one, and the domain will not accept a
    second supplier rate at that point - so this is a revision of our own price
    and asking for theirs again would be asking for something unusable.
    """
    order_id = int((query.data or "").split(":")[2])

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        ops_chat, _ = ctx
        order = await session.get(FxOrder, order_id)
        if order is None:
            await query.answer("That deal no longer exists.", show_alert=True)
            return
        reference = order.display_reference
        already_quoted = order.status is FxOrderStatus.RATE_QUOTED
        supplier_rate = order.supplier_rate
        requests = [] if already_quoted else await _supplier_requests(
            session, ops_chat.department
        )
        markup = _supplier_keyboard(order_id, requests) if requests else None

    await query.answer()

    if already_quoted:
        # Our price only. Said plainly rather than silently skipping a step,
        # because a desk that expected to be asked for the supplier's rate
        # should know why it was not.
        await state.set_state(FxQuote.awaiting_client_rate)
        await state.update_data(
            order_id=order_id,
            supplier_work_item_id=None,
            supplier_rate=str(supplier_rate) if supplier_rate is not None else None,
        )
        text, markup, mode = prompt_for(
            query.from_user,
            f"{reference} already has the supplier's rate recorded "
            f"({fx.format_money(supplier_rate)}), so this changes our price to "
            f"the client only. What are we quoting?",
            placeholder="Our rate",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        return

    if markup is None:
        await query.message.answer(
            f"{reference} has no supplier request to price against. Raise one "
            f"with /{cmd.NEW_SUPPLIER} first — that is the message asking them "
            f"for a rate, and the deal is priced against it."
        )
        return

    await state.clear()
    await query.message.answer(
        f"{reference} — which supplier request is this price against?",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("fx:qsup:"))
async def quote_pick_supplier(query: CallbackQuery, state: FSMContext) -> None:
    _, _, order_id, work_item_id = (query.data or "").split(":")

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        ops_chat, _ = ctx
        # Re-resolved against the department rather than trusted. A callback
        # carries whatever id it was built with, and this one decides which
        # group an order will later be sent to.
        allowed = await _supplier_requests(session, ops_chat.department)
        chosen = next(
            ((i, c) for i, c in allowed if i.id == int(work_item_id)), None
        )
        if chosen is None:
            await query.answer(
                "That request is not one this department can price against.",
                show_alert=True,
            )
            return
        item, supplier = chosen
        supplier_name = supplier.name

    await state.set_state(FxQuote.awaiting_supplier_rate)
    await state.update_data(
        order_id=int(order_id),
        supplier_work_item_id=item.id,
        supplier_name=supplier_name,
    )
    text, markup, mode = prompt_for(
        query.from_user,
        f"What rate did {supplier_name} quote us?",
        placeholder="Their rate",
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)
    await query.answer()


@router.message(FxQuote.awaiting_supplier_rate)
async def capture_supplier_rate(message: Message, state: FSMContext) -> None:
    try:
        rate = fx.parse_rate(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    await state.update_data(supplier_rate=str(rate))
    await state.set_state(FxQuote.awaiting_client_rate)
    text, markup, mode = prompt_for(
        message.from_user,
        "And what are we quoting the client?",
        placeholder="Our rate",
    )
    await message.answer(text, reply_markup=markup, parse_mode=mode)


@router.message(FxQuote.awaiting_client_rate)
async def capture_client_rate(message: Message, state: FSMContext) -> None:
    try:
        client_rate = fx.parse_rate(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    data = await state.get_data()
    raw = data.get("supplier_rate")
    supplier_rate = Decimal(raw) if raw is not None else None
    warning = check_margin(supplier_rate, client_rate)

    await state.update_data(client_rate=str(client_rate))

    async with session_scope() as session:
        order = await session.get(FxOrder, data["order_id"])
        if order is None:
            await state.clear()
            await message.reply("That deal no longer exists.")
            return
        reference = order.display_reference

    if warning:
        await message.answer(f"⚠ {warning}")

    await message.answer(
        quote_preview(
            reference,
            data.get("supplier_name") or "The supplier",
            supplier_rate if data.get("supplier_work_item_id") else None,
            client_rate,
        ),
        reply_markup=_action_keyboard(
            "✅ Save these rates", f"fx:qsave:{data['order_id']}"
        ),
    )


@router.callback_query(F.data.startswith("fx:qsave:"))
async def quote_save(query: CallbackQuery, state: FSMContext) -> None:
    """Both rates, in one transaction.

    In that order, and together. The supplier's rate has to be on the deal
    before ours is checked against it, and `fx.quote_client` refuses a price
    below cost - a check worth nothing if the two were saved separately and
    somebody stopped halfway.
    """
    order_id = int((query.data or "").split(":")[2])
    data = await state.get_data()
    await query.answer()

    if data.get("order_id") != order_id or "client_rate" not in data:
        await state.clear()
        await query.message.answer("That draft has already been saved, or it expired.")
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.message.answer("You are not registered as staff.")
            return
        _, actor = ctx
        order = await session.get(FxOrder, order_id)
        if order is None:
            await state.clear()
            await query.message.answer("That deal no longer exists.")
            return

        try:
            work_item_id = data.get("supplier_work_item_id")
            if work_item_id is not None:
                supplier_item = await session.get(WorkItem, work_item_id)
                supplier = await session.get(Client, supplier_item.client_id)
                await fx.record_supplier_quote(
                    session, order,
                    supplier=supplier,
                    supplier_work_item=supplier_item,
                    rate=Decimal(data["supplier_rate"]),
                    actor=actor,
                )
            await fx.quote_client(
                session, order, rate=Decimal(data["client_rate"]), actor=actor
            )
            reference, status = order.display_reference, order.status
        except Exception as exc:
            logger.exception("FX quote failed for %s", order_id)
            await query.message.answer(explain(exc))
            return

    await state.clear()
    await query.message.answer(
        f"{reference} is now {status.label}.\n\n"
        f"Tell the client the rate in their group, then use /{cmd.ORDER_CLIENT} "
        f"to create their order."
    )


# --------------------------------------------------------------------------
# `/nphash` - step 10
# --------------------------------------------------------------------------

@router.message(cmd.any_case(cmd.HASH))
async def settle(message: Message, state: FSMContext) -> None:
    """`/nphash` - the supplier has sent the money, and here is the proof."""
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
        deals = await _open_deals(session, allowed=(FxOrderStatus.AWAITING_SETTLEMENT,))
        markup = _pick_keyboard(deals, "hdeal") if deals else None

    if not deals:
        await message.reply(
            "No deal is waiting on settlement. A deal reaches that point once "
            "the supplier has accepted their order."
        )
        return

    await state.clear()
    await message.reply("Which deal has settled?", reply_markup=markup)


@router.callback_query(F.data.startswith("fx:hdeal:"))
async def settle_pick_deal(query: CallbackQuery, state: FSMContext) -> None:
    order_id = int((query.data or "").split(":")[2])

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        order = await session.get(FxOrder, order_id)
        if order is None:
            await query.answer("That deal no longer exists.", show_alert=True)
            return
        reference = order.display_reference

    await state.set_state(FxHash.awaiting_hash)
    await state.update_data(order_id=order_id)
    text, markup, mode = prompt_for(
        query.from_user,
        f"{reference} — paste the transaction hash. It goes to the client as "
        f"proof, so paste it rather than typing it.",
        placeholder="Transaction hash",
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)
    await query.answer()


@router.message(FxHash.awaiting_hash)
async def capture_hash(message: Message, state: FSMContext) -> None:
    """Checked here with the same function the domain uses to check it.

    Not a second implementation of "does this look like a hash". The point of
    checking early is to catch a truncated paste while the person still has the
    real one on their clipboard, and a check that disagreed with the one at the
    end would be worse than no check at all.
    """
    try:
        tx_hash = fx.check_hash(message.text or "")
    except fx.FxError as exc:
        await message.reply(str(exc))
        return

    data = await state.get_data()
    async with session_scope() as session:
        order = await session.get(FxOrder, data["order_id"])
        if order is None:
            await state.clear()
            await message.reply("That deal no longer exists.")
            return
        reference, chain = order.display_reference, order.chain

    await state.update_data(tx_hash=tx_hash)
    link = fx.explorer_link(chain, tx_hash)
    lines = [
        f"{reference} — settlement, not yet sent",
        "",
        tx_hash,
    ]
    if link:
        lines.append(link)
    lines += [
        "",
        "Sending this passes it to the client with the amount they receive, "
        "and asks them to confirm the funds have arrived. The supplier's side "
        "of the deal is not mentioned.",
    ]

    await message.answer(
        "\n".join(lines),
        reply_markup=_action_keyboard(
            "✉ Send to the client", f"fx:hsend:{data['order_id']}"
        ),
    )


@router.callback_query(F.data.startswith("fx:hsend:"))
async def settle_send(query: CallbackQuery, state: FSMContext) -> None:
    order_id = int((query.data or "").split(":")[2])
    data = await state.get_data()
    await query.answer()

    if data.get("order_id") != order_id or "tx_hash" not in data:
        await state.clear()
        await query.message.answer("That draft has already been sent, or it expired.")
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.message.answer("You are not registered as staff.")
            return
        _, actor = ctx
        order = await session.get(FxOrder, order_id)
        if order is None:
            await state.clear()
            await query.message.answer("That deal no longer exists.")
            return

        try:
            await fx.record_hash(session, order, tx_hash=data["tx_hash"], actor=actor)
            await fx_relay.send_settlement(
                session, gateway(), order, actor=actor,
                keyboard=receipt_keyboard(order.id),
            )
        except Exception as exc:
            logger.exception("FX settlement send failed for %s", order_id)
            await query.message.answer(explain(exc))
            return

    await state.clear()
    await query.message.answer("Sent to the client, with a button to confirm receipt.")


# --------------------------------------------------------------------------
# `/npreject` - the two return paths
# --------------------------------------------------------------------------

@router.message(cmd.any_case(cmd.REJECT))
async def reject(message: Message, state: FSMContext) -> None:
    """`/npreject` - a rate turned down, by us or by the client.

    One command for both, unlike the order commands, and for the opposite
    reason: which side is being rejected is decided by where the deal already
    is rather than by what the desk intends, so asking would be asking a
    question the bot can already answer.
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
        deals = [d for d in await _open_deals(session) if rejectable_sides(d)]
        markup = _pick_keyboard(deals, "rdeal") if deals else None

    if not deals:
        await message.reply(
            "No deal has a rate that can be turned down. There has to be a "
            "price on the table first."
        )
        return

    await state.clear()
    await message.reply("Which deal?", reply_markup=markup)


@router.callback_query(F.data.startswith("fx:rdeal:"))
async def reject_pick_deal(query: CallbackQuery, state: FSMContext) -> None:
    order_id = int((query.data or "").split(":")[2])

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        order = await session.get(FxOrder, order_id)
        if order is None:
            await query.answer("That deal no longer exists.", show_alert=True)
            return
        sides = rejectable_sides(order)
        reference = order.display_reference

    await query.answer()

    if not sides:
        await query.message.answer(
            f"{reference} has moved on and no longer has a rate to turn down."
        )
        return

    if len(sides) == 1:
        await _ask_reason(query, state, order_id, sides[0])
        return

    await query.message.answer(
        f"{reference} — which way round?",
        reply_markup=_side_keyboard(order_id, sides),
    )


@router.callback_query(F.data.startswith("fx:rside:"))
async def reject_pick_side(query: CallbackQuery, state: FSMContext) -> None:
    _, _, order_id, side_value = (query.data or "").split(":")
    await query.answer()
    await _ask_reason(query, state, int(order_id), FxSide(side_value))


async def _ask_reason(
    query: CallbackQuery, state: FSMContext, order_id: int, side: FxSide
) -> None:
    await state.set_state(FxReject.awaiting_reason)
    await state.update_data(order_id=order_id, side=side.value)
    text, markup, mode = prompt_for(
        query.from_user, reject_prompt(side), placeholder="The reason",
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)


@router.message(FxReject.awaiting_reason)
async def capture_reason(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.reply("Give me a reason — it is what the history will show.")
        return

    data = await state.get_data()
    side = FxSide(data["side"])
    await state.update_data(reason=reason)

    async with session_scope() as session:
        order = await session.get(FxOrder, data["order_id"])
        if order is None:
            await state.clear()
            await message.reply("That deal no longer exists.")
            return
        reference = order.display_reference

    if side is FxSide.SUPPLIER:
        consequence = (
            "The supplier is told we cannot work with that rate, and nothing "
            "else — not the reason, and not the rate we go with instead. The "
            "deal stays open for another price."
        )
    else:
        consequence = (
            "Recorded against the deal, and the deal goes back to being "
            "unpriced. Nothing is sent to anybody."
        )

    await message.answer(
        f"{reference} — not yet recorded\n\n{reason}\n\n{consequence}",
        reply_markup=_action_keyboard(
            "✅ Record it", f"fx:rsave:{data['order_id']}:{side.value}"
        ),
    )


@router.callback_query(F.data.startswith("fx:rsave:"))
async def reject_save(query: CallbackQuery, state: FSMContext) -> None:
    _, _, order_id, side_value = (query.data or "").split(":")
    side = FxSide(side_value)
    data = await state.get_data()
    await query.answer()

    if data.get("order_id") != int(order_id) or "reason" not in data:
        await state.clear()
        await query.message.answer("That draft has already been recorded, or it expired.")
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.message.answer("You are not registered as staff.")
            return
        _, actor = ctx
        order = await session.get(FxOrder, int(order_id))
        if order is None:
            await state.clear()
            await query.message.answer("That deal no longer exists.")
            return

        try:
            if side is FxSide.SUPPLIER:
                await fx.reject_supplier_rate(
                    session, order, reason=data["reason"], actor=actor
                )
                await fx_relay.notify_rejected(
                    session, gateway(), order, actor=actor, reason=data["reason"]
                )
                outcome = "Told the supplier, and the deal is open for another price."
            else:
                await fx.reject_client_rate(
                    session, order, reason=data["reason"], actor=actor
                )
                outcome = (
                    f"Recorded. {order.display_reference} is now "
                    f"{order.status.label} — go back to the supplier, then "
                    f"/{cmd.QUOTE} again."
                )
        except Exception as exc:
            logger.exception("FX rejection failed for %s", order_id)
            await query.message.answer(explain(exc))
            return

    await state.clear()
    await query.message.answer(outcome)


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
    "QUOTABLE",
    "Figures",
    "check_consistent",
    "check_margin",
    "confirm_keyboard",
    "default_account_name",
    "parse_pair",
    "preview_text",
    "quote_preview",
    "receipt_keyboard",
    "reject_prompt",
    "rejectable_sides",
    "router",
    "start_deal",
]
