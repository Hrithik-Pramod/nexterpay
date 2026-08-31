"""Composing and sending a broadcast.

Four steps on purpose: write it, choose who gets it, look at exactly what will
be sent and to how many groups, then confirm. Every other action on the
platform reaches one group. This one reaches all of them and cannot be
unsent after 48 hours, so it is the one place where extra friction is the
feature rather than the cost.
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
from app.bot.deps import explain, gateway, refusal_reason, staff_context
from app.db.base import session_scope
from app.db.models import Broadcast
from app.services import broadcast as bc

logger = logging.getLogger(__name__)
router = Router(name="broadcast")


class BroadcastCompose(StatesGroup):
    awaiting_message = State()


def _audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="All clients", callback_data=f"bc:aud:{bc.CLIENTS}")],
            [
                InlineKeyboardButton(
                    text="All suppliers", callback_data=f"bc:aud:{bc.SUPPLIERS}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Everyone", callback_data=f"bc:aud:{bc.EVERYONE}"
                )
            ],
            [InlineKeyboardButton(text="Choose groups…", callback_data="bc:aud:pick")],
            [InlineKeyboardButton(text="Cancel", callback_data="bc:cancel")],
        ]
    )


def _picker(chats, chosen: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'☑' if c.telegram_chat_id in chosen else '☐'} "
                f"{c.title or c.telegram_chat_id}"[:60],
                callback_data=f"bc:tog:{c.telegram_chat_id}",
            )
        ]
        for c in chats
    ]
    rows.append(
        [
            InlineKeyboardButton(text="Continue", callback_data="bc:done"),
            InlineKeyboardButton(text="Cancel", callback_data="bc:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Send broadcast", callback_data="bc:send"),
                InlineKeyboardButton(text="Cancel", callback_data="bc:cancel"),
            ]
        ]
    )


@router.message(Command(cmd.BROADCAST))
async def start(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        ctx = await staff_context(
            session, message.chat.id, message.from_user.id if message.from_user else None
        )
        if ctx is None:
            await message.reply(
                refusal_reason(message.from_user.id if message.from_user else None)
            )
            return
        _, actor = ctx
        try:
            # Checked here as well as at send time so nobody composes a message
            # they were never going to be allowed to send.
            actor.require(bc.ROLE_REQUIRED_TO_BROADCAST)
        except Exception as exc:
            await message.reply(explain(exc))
            return

    await state.set_state(BroadcastCompose.awaiting_message)
    await state.update_data(selected=[])
    await message.reply(
        "Type the message you want to broadcast. You will choose who receives "
        "it, and see it in full, before anything is sent.",
        reply_markup=ForceReply(selective=True),
    )


@router.message(BroadcastCompose.awaiting_message)
async def capture(message: Message, state: FSMContext) -> None:
    body = (message.text or message.caption or "").strip()
    if not body:
        await message.reply("A broadcast needs some words. Type the message, or ignore this.")
        return

    await state.update_data(body=body)
    await message.reply("Who should receive this?", reply_markup=_audience_keyboard())


@router.callback_query(F.data.startswith("bc:aud:"))
async def choose_audience(query: CallbackQuery, state: FSMContext) -> None:
    audience = (query.data or "").split(":")[2]
    data = await state.get_data()
    body = data.get("body")
    if not body:
        await query.answer("That draft has expired. Start again.", show_alert=True)
        return

    if audience == "pick":
        async with session_scope() as session:
            chats = await _counterparty_chats(session)
        if not chats:
            await query.answer("No counterparty groups are registered.", show_alert=True)
            return
        await query.message.edit_text(
            "Choose the groups, then tap Continue.",
            reply_markup=_picker(chats, set(data.get("selected", []))),
        )
        await query.answer()
        return

    await state.update_data(audience=audience)
    await _show_preview(query, state, audience, body)


@router.callback_query(F.data.startswith("bc:tog:"))
async def toggle(query: CallbackQuery, state: FSMContext) -> None:
    chat_id = int((query.data or "").split(":")[2])
    data = await state.get_data()
    chosen = set(data.get("selected", []))
    chosen.symmetric_difference_update({chat_id})
    await state.update_data(selected=sorted(chosen))

    async with session_scope() as session:
        chats = await _counterparty_chats(session)
    await query.message.edit_reply_markup(reply_markup=_picker(chats, chosen))
    await query.answer()


@router.callback_query(F.data == "bc:done")
async def finish_picking(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("selected"):
        await query.answer("Choose at least one group.", show_alert=True)
        return
    await state.update_data(audience=bc.SELECTED)
    await _show_preview(query, state, bc.SELECTED, data["body"])


async def _show_preview(query: CallbackQuery, state: FSMContext, audience, body) -> None:
    data = await state.get_data()
    async with session_scope() as session:
        recipients = await bc.audience_for(session, audience, data.get("selected"))
    if not recipients:
        await query.answer("That audience has no groups in it.", show_alert=True)
        return

    await state.update_data(recipients=[r.telegram_chat_id for r in recipients])
    await query.message.edit_text(
        bc.preview(body, audience, recipients)[:4000], reply_markup=_confirm()
    )
    await query.answer()


@router.callback_query(F.data == "bc:send")
async def send(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    body, audience = data.get("body"), data.get("audience")
    if not body or not audience:
        await query.answer("That draft has expired. Start again.", show_alert=True)
        return

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id, query.from_user.id if query.from_user else None
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        _, actor = ctx

        recipients = await bc.audience_for(session, audience, data.get("selected"))
        try:
            record = await bc.send(
                session, gateway(), body=body, audience=audience,
                recipients=recipients, actor=actor,
            )
            summary = bc.outcome(record, await bc.deliveries_for(session, record))
            broadcast_id = record.id
        except Exception as exc:
            await query.answer(explain(exc)[:190], show_alert=True)
            return

    await state.clear()
    try:
        await query.message.edit_text(
            f"{summary}\n\n— — —\n{body}"[:4000],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Recall this broadcast",
                        callback_data=f"bc:recall:{broadcast_id}",
                    )
                ]]
            ),
        )
    except Exception:
        logger.debug("Could not update the broadcast preview", exc_info=True)
    await query.answer("Sent")


@router.callback_query(F.data.startswith("bc:recall:"))
async def recall(query: CallbackQuery) -> None:
    broadcast_id = int((query.data or "").split(":")[2])
    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id, query.from_user.id if query.from_user else None
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        _, actor = ctx
        record = await session.get(Broadcast, broadcast_id)
        if record is None:
            await query.answer("That broadcast no longer exists.", show_alert=True)
            return
        try:
            result = await bc.recall(session, gateway(), record, actor)
        except Exception as exc:
            await query.answer(explain(exc)[:190], show_alert=True)
            return

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear the recall button", exc_info=True)
    await query.answer(result[:190], show_alert=True)


@router.callback_query(F.data == "bc:cancel")
async def cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.message.edit_text("Cancelled. Nothing was sent.")
    await query.answer()


async def _counterparty_chats(session):
    from sqlalchemy import select

    from app.db.models import Chat
    from app.domain.enums import ChatKind

    result = await session.execute(
        select(Chat)
        .where(Chat.is_active.is_(True), Chat.kind == ChatKind.CLIENT)
        .order_by(Chat.title)
    )
    return list(result.scalars().all())
