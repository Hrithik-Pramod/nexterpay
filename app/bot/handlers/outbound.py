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
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot import commands as cmd
from app.bot import keyboards as kb
from app.bot.deps import explain, gateway, refusal_reason, staff_context
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


def _confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✉ Send and open", callback_data="ob:send"),
                InlineKeyboardButton(text="Cancel", callback_data="ob:cancel"),
            ]
        ]
    )


async def _counterparties(session, department):
    """Groups this department can raise into.

    Scoped to the department deliberately. Support staff have no business
    opening a request in a Finance client's group, and an unscoped list is
    how that happens by accident on a busy afternoon.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(Chat)
        .where(
            Chat.is_active.is_(True),
            Chat.kind == ChatKind.CLIENT,
            Chat.department == department,
        )
        .order_by(Chat.title)
    )
    chats = list(result.scalars().all())
    for chat in chats:
        await session.refresh(chat, ["client"])
    return chats


@router.message(Command(cmd.NEW))
async def start(message: Message, state: FSMContext) -> None:
    """`/np_new` - open a request with a client or supplier."""
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
        options = await _counterparties(session, chat.department)
        markup = _counterparty_keyboard(options) if options else None

    if not options:
        await message.reply(
            f"No client or supplier groups are registered for "
            f"{chat.department.label} yet."
        )
        return

    await state.clear()
    await message.reply("Who is this request with?", reply_markup=markup)


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
    await query.message.answer(
        f"Raising a request with {title}. Type it below - you will see it "
        f"before anything is sent.",
        reply_markup=ForceReply(selective=True),
    )
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

    await message.reply(
        f"This will open a new request with {data.get('to_title')} and send:\n\n"
        f"— — —\n{subject}\n\n{body}\n— — —\n\n"
        f"Nothing has been sent yet.",
        reply_markup=_confirm(),
    )


@router.callback_query(F.data == "ob:send")
async def send(query: CallbackQuery, state: FSMContext) -> None:
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
