"""Client-side handlers: raising requests, and replies coming back.

The client experience is deliberately thin (PRD 8.3). One button, one prompt,
then everything else is ordinary Telegram conversation.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, Message

from app.bot import keyboards as kb
from app.bot.attachments import extract_attachments
from app.bot.deps import client_context, gateway
from app.bot.routing import IncomingMessage, build_strategy
from app.config import get_settings
from app.db.base import session_scope
from app.services import relay

logger = logging.getLogger(__name__)
router = Router(name="client")


class RaiseRequest(StatesGroup):
    awaiting_details = State()


@router.message(Command("raise", "request", "enquiry"))
async def offer_raise_button(message: Message) -> None:
    async with session_scope() as session:
        chat = await client_context(session, message.chat.id)
        if chat is None:
            return
        department = chat.department.value

    prompt = (
        "What would you like to discuss?"
        if department == "business"
        else "What do you need help with?"
    )
    await message.answer(prompt, reply_markup=kb.raise_request_prompt(department))


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
    # it. It does deliver replies to the bot's own messages. Asking the client
    # to type freely would mean the answer never arrives; asking them to reply
    # means it always does - and Telegram opens the reply box for them, so it
    # costs the client nothing.
    #
    # If this is ever changed to a plain send, the Raise Request flow silently
    # stops working in any group where the bot is not an admin.
    mention = (
        f"{query.from_user.full_name}, {ask[0].lower()}{ask[1:]}"
        if query.from_user
        else ask
    )
    await query.message.answer(mention, reply_markup=ForceReply(selective=True))
    await query.answer()


@router.message(RaiseRequest.awaiting_details)
async def capture_request(message: Message, state: FSMContext) -> None:
    body = (message.text or message.caption or "").strip()
    if not body and not extract_attachments(message):
        await message.reply("Please include a description of what you need.")
        return

    await state.clear()
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
            logger.info(
                "Unrouted client message in chat %s (not a reply to one of ours)",
                message.chat.id,
            )
            return

        await relay.relay_client_message(
            session, gateway(), item,
            text=incoming.text,
            sender_name=incoming.sender_name,
            telegram_message_id=incoming.telegram_message_id,
            sender_telegram_user_id=incoming.sender_telegram_user_id,
            attachments=extract_attachments(message),
        )
