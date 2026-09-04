"""Client-side handlers: raising requests, and replies coming back.

The client experience is deliberately thin (PRD 8.3). One button, one prompt,
then everything else is ordinary Telegram conversation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot import commands as cmd
from app.bot import keyboards as kb
from app.bot.attachments import extract_attachments
from app.bot.deps import client_context, gateway, prompt_for
from app.bot.routing import IncomingMessage, build_strategy
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import WorkItem
from app.services import broadcast as broadcast_service
from app.services import relay

logger = logging.getLogger(__name__)
router = Router(name="client")


class RaiseRequest(StatesGroup):
    awaiting_details = State()


@router.message(cmd.any_case(cmd.FRONT_DOOR, cmd.RAISE, cmd.REQUEST, cmd.ENQUIRY))
async def offer_raise_button(message: Message, command: CommandObject) -> None:
    """`/np_raise <description>` opens a request outright; `/np` asks.

    `/np` on its own is the front door - the single command clients are asked
    to remember. It lands here rather than in its own handler because the
    answer is the same either way: show them the button.

    The one-line form exists because it is the only path that cannot be
    defeated by privacy mode: a command always reaches the bot, whereas an
    ordinary follow-up message does not. Everything else here is convenience
    layered on top of it.
    """
    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return
        department = chat.department.value

    described = (command.args or "").strip()
    if described:
        await _open_from(message, described)
        return

    prompt = (
        "What would you like to discuss?"
        if department == "business"
        else "What do you need help with?"
    )
    await message.answer(
        f"{prompt}\n\nTap the button below, or send it in one go - "
        f"for example: /{cmd.RAISE} payment not received for INV-2041",
        reply_markup=kb.raise_request_prompt(department),
    )


@router.callback_query(F.data == "raise:new")
async def start_request(query: CallbackQuery, state: FSMContext) -> None:
    async with session_scope() as session:
        chat = await client_context(session, query.message.chat.id)
        if chat is None:
            await query.answer()
            return
        department = chat.department.value

    await state.set_state(RaiseRequest.awaiting_details)
    await state.update_data(chat_id=query.message.chat.id)

    ask = (
        "Please describe what you would like to discuss. Include as much detail "
        "as you can, and attach any documents that would help."
        if department == "business"
        else "Please describe the issue, including any reference numbers or "
             "screenshots that would help us investigate."
    )

    # ForceReply is load-bearing, not decoration.
    #
    # The bot runs with privacy mode ON and is deliberately NOT an administrator
    # in client groups, so Telegram does not deliver ordinary group messages to
    # it. It does deliver replies to the bot's own messages, which is why the
    # description has to arrive as a reply. Telegram's own documentation makes
    # the same recommendation: "using the force reply option for the bot's
    # messages should be more than enough."
    #
    # The mention is built by deps.prompt_for, which explains why it has to be
    # a tg://user link rather than a plain name. This was the only place that
    # got it right; three other handlers got it wrong, so it now lives in one.
    text, markup, mode = prompt_for(
        query.from_user, ask, placeholder="Describe your request"
    )
    await query.message.answer(text, reply_markup=markup, parse_mode=mode)
    await query.answer()


@router.message(RaiseRequest.awaiting_details)
async def capture_request(message: Message, state: FSMContext) -> None:
    body = (message.text or message.caption or "").strip()
    if not body and not extract_attachments(message):
        await message.reply("Please include a description of what you need.")
        return

    await state.clear()
    await _open_from(message, body)


def _broadcast_context(record) -> str:
    """What the team sees at the top of a request raised from a broadcast.

    Without it the topic can read "why so?" and nothing else, which tells
    whoever picks it up nothing at all.
    """
    when = record.created_at.strftime("%d %b %H:%M") if record.created_at else "earlier"
    quoted = " ".join((record.body or "").split())
    if len(quoted) > 300:
        quoted = quoted[:299].rstrip() + "…"
    return (
        f"↳ Raised in reply to the broadcast sent {when} by {record.sent_by_name}:\n"
        f'"{quoted}"'
    )


async def _open_from(message: Message, body: str, *, context: str | None = None) -> None:
    """Create the work item and post its action keyboard. The single path in."""
    subject = (body.splitlines()[0] if body else "Attachment")[:120] or "New request"

    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return

        item = await relay.open_request(
            session,
            gateway(),
            source_chat=chat,
            subject=subject,
            body=body or "(no text - see attachment)",
            raised_by_name=message.from_user.full_name if message.from_user else "Client",
            raised_by_telegram_user_id=message.from_user.id if message.from_user else None,
            attachments=extract_attachments(message),
            keyboard=None,  # attached after creation, once the id exists
            ack_keyboard=kb.acknowledgement_actions(),
            context=context,
        )
        work_item_id = item.id
        ops_chat_id = (await relay.chats_for(session, item))[1].telegram_chat_id
        thread_id = item.topic_id

    # Action keyboard, now that the work item has an id to embed in callbacks.
    await gateway().send_message(
        ops_chat_id,
        "Actions:",
        thread_id=thread_id,
        reply_markup=kb.work_item_actions(work_item_id, claimed=False),
    )


@router.message(cmd.any_case(cmd.TICKETS))
async def cmd_tickets(message: Message) -> None:
    """`/np_tickets` - the client's own open requests."""
    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return
        items = await relay.open_requests_for(session, chat, recent_closed=True)
        lines = [
            f"{i.client_reference} · {i.subject}\n    {i.status.client_label}"
            for i in items
        ]
        markup = kb.open_requests(items) if items else None

    if not items:
        await message.answer(
            "You have nothing open, and nothing resolved in the last four weeks. "
            f"Send /{cmd.FRONT_DOOR} to raise one."
        )
        return
    await message.answer(
        "Your requests:\n\n"
        + "\n\n".join(lines)
        + "\n\nOpen ones, plus anything resolved in the last four weeks. "
        "Tap one to add to it.",
        reply_markup=markup,
    )


@router.callback_query(F.data == "tk:list")
async def show_requests(query: CallbackQuery) -> None:
    async with session_scope() as session:
        chat = await client_context(session, query.message.chat.id)
        if chat is None:
            await query.answer()
            return
        items = await relay.open_requests_for(session, chat, recent_closed=True)
        lines = [
            f"{i.client_reference} · {i.subject}\n    {i.status.client_label}"
            for i in items
        ]
        markup = kb.open_requests(items) if items else None

    if not items:
        await query.answer("You have no recent requests.", show_alert=True)
        return
    await query.message.answer(
        "Your requests:\n\n"
        + "\n\n".join(lines)
        + "\n\nOpen ones, plus anything resolved in the last four weeks. "
        "Tap one to add to it.",
        reply_markup=markup,
    )
    await query.answer()


@router.callback_query(F.data.startswith("tk:open:"))
async def open_one_request(query: CallbackQuery) -> None:
    """Post a fresh anchor for the chosen request, at the bottom of the chat."""
    try:
        work_item_id = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    async with session_scope() as session:
        chat = await client_context(session, query.message.chat.id)
        item = await session.get(WorkItem, work_item_id)
        # Checked rather than assumed: a callback carries whatever id it was
        # built with, and this one must belong to the group it was tapped in.
        if chat is None or item is None or item.source_chat_id != chat.id:
            await query.answer("That request is not from this group.", show_alert=True)
            return
        await relay.post_anchor(session, gateway(), item)

    await query.answer()


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def client_reply(message: Message) -> None:
    """Anything else a client says in their group.

    Resolved through the configured routing strategy. If it resolves to
    nothing, we do nothing - see the note in `app/bot/routing.py`.
    """
    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return

        strategy = build_strategy(get_settings().reply_routing_strategy)
        incoming = IncomingMessage(
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            sender_name=message.from_user.full_name if message.from_user else "Client",
            sender_telegram_user_id=message.from_user.id if message.from_user else None,
            text=message.text or message.caption,
            reply_to_message_id=(
                message.reply_to_message.message_id if message.reply_to_message else None
            ),
        )
        item = await strategy.resolve(session, chat, incoming)
        if item is None:
            # A reply to a broadcast resolves to nothing, because a broadcast
            # is not a ticket. NexterPay asked that it open a fresh request
            # rather than disappear - a client answering a message we sent
            # them should never go unanswered.
            replied_to = (
                await broadcast_service.broadcast_behind(
                    session, message.chat.id, incoming.reply_to_message_id
                )
                if incoming.reply_to_message_id is not None
                else None
            )
            if replied_to is not None:
                opened_from_broadcast = _broadcast_context(replied_to)
            else:
                logger.info(
                    "Unrouted client message in chat %s (not a reply to one of ours)",
                    message.chat.id,
                )
                return
        else:
            opened_from_broadcast = None

    if opened_from_broadcast:
        body = (message.text or message.caption or "").strip()
        if body or extract_attachments(message):
            await _open_from(message, body, context=opened_from_broadcast)
        return

    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return
        item = await strategy.resolve(session, chat, incoming)
        if item is None:
            return

        await relay.relay_client_message(
            session, gateway(), item,
            text=incoming.text,
            sender_name=incoming.sender_name,
            telegram_message_id=incoming.telegram_message_id,
            sender_telegram_user_id=incoming.sender_telegram_user_id,
            attachments=extract_attachments(message),
        )
