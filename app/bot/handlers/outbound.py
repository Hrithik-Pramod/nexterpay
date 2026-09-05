"""Raising a request outbound, from NexterPay to a client or supplier.

The mirror of a client tapping Raise Request. Same four steps as everything
else that reaches an outside party: choose who, write it, look at it, confirm.

Registered in front of the staff router. Composing this is answered as a
reply, and in a forum a reply carries a thread id, which the staff topic
catch-all would otherwise consume before this ever sees it.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot import commands as cmd
from app.bot import keyboards as kb
from app.bot.deps import (
    explain,
    gateway,
    prompt_for,
    refusal_reason,
    staff_context,
)
from app.bot.registry import leads_for, resolve_chat
from app.db.base import session_scope
from app.db.models import Chat
from app.domain.enums import ChatKind
from app.services import relay

logger = logging.getLogger(__name__)
router = Router(name="outbound")


class OutboundCompose(StatesGroup):
    awaiting_message = State()


def _counterparty_keyboard(chats) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{(c.client.code or '????') if c.client else '????'} · "
                f"{c.title or c.telegram_chat_id}"[:60],
                callback_data=f"ob:to:{c.telegram_chat_id}",
            )
        ]
        for c in chats
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="ob:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm(lead=None) -> InlineKeyboardMarkup:
    """Send to the room, or send addressed to the named contact.

    The same two-button shape as replying, and for the same reason. NexterPay
    asked where the choice fits: it is here, after the group is chosen and the
    message typed, because until then there is no lead to offer - the contact
    belongs to the group, so it cannot be known before one is picked.

    The tag button only appears when somebody has been named for that group.
    A button that would do nothing needs explaining, and explaining it is
    worse than not offering it.
    """
    rows = [[InlineKeyboardButton(text="✉ Send and open", callback_data="ob:send")]]
    if lead is not None:
        rows.append(
            [InlineKeyboardButton(
                text=f"✉ Send and tag {lead.display_name}"[:60],
                callback_data="ob:sendtag",
            )]
        )
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="ob:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _counterparties(session, department, *, suppliers: bool | None = None):
    """Groups this department can raise into.

    Scoped to the department deliberately. Support staff have no business
    opening a request in a Finance client's group, and an unscoped list is
    how that happens by accident on a busy afternoon.

    `suppliers` narrows it further to one side of the trade. None returns
    both, which nothing uses any more but the tests still exercise.
    """
    from sqlalchemy import select

    query = select(Chat).where(
        Chat.is_active.is_(True),
        Chat.kind == ChatKind.CLIENT,
        Chat.department == department,
    )
    if suppliers is not None:
        query = query.where(Chat.is_supplier.is_(suppliers))

    result = await session.execute(query.order_by(Chat.title))
    chats = list(result.scalars().all())
    for chat in chats:
        await session.refresh(chat, ["client"])
    return chats


@router.message(cmd.any_case(cmd.NEW_CLIENT))
async def start_with_client(message: Message, state: FSMContext) -> None:
    """`/npnewcl` - open a request with a client."""
    await _start(message, state, suppliers=False, noun="client")


@router.message(cmd.any_case(cmd.NEW_SUPPLIER))
async def start_with_supplier(message: Message, state: FSMContext) -> None:
    """`/npnewsu` - open a request with a supplier."""
    await _start(message, state, suppliers=True, noun="supplier")


async def _start(
    message: Message, state: FSMContext, *, suppliers: bool, noun: str
) -> None:
    """One flow, two doors.

    This was a single command with a mixed picker until NexterPay pointed out
    that the picker was where you found out which kind of counterparty you
    were about to open a conversation with. Putting it in the command means
    the decision is made before the list appears, not from it.
    """
    async with session_scope() as session:
        ctx = await staff_context(
            session, message.chat.id, message.from_user.id if message.from_user else None
        )
        if ctx is None:
            await message.reply(
                await refusal_reason(
                    message.from_user.id if message.from_user else None,
                    session, message.chat.id,
                )
            )
            return
        chat, _ = ctx
        options = await _counterparties(session, chat.department, suppliers=suppliers)
        markup = _counterparty_keyboard(options) if options else None

    if not options:
        other = cmd.NEW_CLIENT if suppliers else cmd.NEW_SUPPLIER
        await message.reply(
            f"No {noun} groups are registered for {chat.department.label} yet. "
            f"If you meant the other side of the trade, use /{other}."
        )
        return

    await state.clear()
    await message.reply(
        f"Which {noun} is this request with?", reply_markup=markup
    )


@router.callback_query(F.data.startswith("ob:to:"))
async def choose_counterparty(query: CallbackQuery, state: FSMContext) -> None:
    telegram_chat_id = int((query.data or "").split(":")[2])

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id, query.from_user.id if query.from_user else None
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        ops_chat, _ = ctx
        options = await _counterparties(session, ops_chat.department)
        # Re-resolved rather than trusted: a callback carries whatever id it
        # was built with, and this one has to belong to the department of the
        # group it was tapped in.
        chosen = next(
            (c for c in options if c.telegram_chat_id == telegram_chat_id), None
        )
        if chosen is None:
            await query.answer(
                "That group is not one this department can raise with.", show_alert=True
            )
            return
        title = chosen.title or str(chosen.telegram_chat_id)

    await state.set_state(OutboundCompose.awaiting_message)
    await state.update_data(to_chat_id=telegram_chat_id, to_title=title)
    text, markup, mode = prompt_for(
        query.from_user,
        f"Raising a request with {title}. Type it below - you will see it "
        f"before anything is sent.",
        placeholder="The request",
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)
    await query.answer()


@router.message(OutboundCompose.awaiting_message)
async def capture(message: Message, state: FSMContext) -> None:
    body = (message.text or message.caption or "").strip()
    if not body:
        await message.reply("Type the request, or tap Cancel on the message above.")
        return

    data = await state.get_data()
    await state.update_data(body=body)
    subject = (body.splitlines()[0] if body else "")[:120] or "New request"

    # The contact for the group they picked, if one has been named.
    lead = None
    async with session_scope() as session:
        target = await resolve_chat(session, data.get("to_chat_id"))
        if target is not None:
            leads = await leads_for(session, target)
            lead = leads[0] if leads else None

    await message.reply(
        f"This will open a new request with {data.get('to_title')} and send:\n\n"
        f"— — —\n{subject}\n\n{body}\n— — —\n\n"
        f"Nothing has been sent yet.",
        reply_markup=_confirm(lead),
    )


@router.callback_query(F.data.in_({"ob:send", "ob:sendtag"}))
async def send(query: CallbackQuery, state: FSMContext) -> None:
    tag = (query.data or "") == "ob:sendtag"
    data = await state.get_data()
    body, to_chat_id = data.get("body"), data.get("to_chat_id")
    if not body or not to_chat_id:
        await query.answer("That draft has expired. Start again.", show_alert=True)
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id, query.from_user.id if query.from_user else None
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        ops_chat, actor = ctx

        options = await _counterparties(session, ops_chat.department)
        target = next((c for c in options if c.telegram_chat_id == to_chat_id), None)
        if target is None:
            await query.answer("That group is no longer available.", show_alert=True)
            return

        subject = (body.splitlines()[0] if body else "")[:120] or "New request"
        try:
            item = await relay.open_outbound(
                session, gateway(),
                counterparty_chat=target, subject=subject, body=body, actor=actor,
                tag_lead=tag,
            )
            reference, work_item_id, thread_id = (
                item.display_reference, item.id, item.topic_id
            )
        except Exception as exc:
            await query.answer(explain(exc)[:190], show_alert=True)
            return

    await state.clear()
    await gateway().send_message(
        query.message.chat.id,
        "Actions:",
        thread_id=thread_id,
        reply_markup=kb.work_item_actions(work_item_id, claimed=False),
    )
    try:
        await query.message.edit_text(
            f"Opened {reference} with {data.get('to_title')} and sent.\n\n— — —\n{body}"[:4000],
            reply_markup=None,
        )
    except Exception:
        logger.debug("Could not update the outbound preview", exc_info=True)
    await query.answer(f"Opened {reference}")


@router.callback_query(F.data == "ob:cancel")
async def cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text("Cancelled. Nothing was sent and no request was opened.")
    await query.answer()
