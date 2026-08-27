"""Staff handlers, used inside the departmental Operations Groups.

The safety property this file exists to preserve: typing in a topic is
internal. Only `/reply` sends to a client. There is no configuration that
changes this and no code path that relays a plain message outward.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, Message

from app.bot import keyboards as kb
from app.bot.attachments import extract_attachments, has_attachment
from app.bot.deps import (
    explain,
    gateway,
    refusal_reason,
    staff_context,
    work_item_for_thread,
)
from app.bot.registry import resolve_chat
from app.db.base import session_scope
from app.db.models import Client, Staff, WorkItem
from app.domain.enums import ChatKind, Priority, WorkItemStatus
from app.domain.history import load_events, render_history
from app.domain.work_items import ROLE_REQUIRED_TO_REASSIGN
from app.services import relay

logger = logging.getLogger(__name__)
router = Router(name="staff")


async def _resolve(session, message_or_query, thread_id) -> tuple | None:
    chat_id = message_or_query.chat.id
    user = message_or_query.from_user
    ctx = await staff_context(session, chat_id, user.id if user else None)
    if ctx is None:
        return None
    chat, actor = ctx
    item = await work_item_for_thread(session, chat, thread_id)
    return chat, actor, item


@router.message(Command("reply"))
async def cmd_reply(message: Message, command: CommandObject) -> None:
    """The only route from an Operations Group to a client."""
    text = (command.args or "").strip()
    if not text:
        await message.reply("Usage: /reply <message to the client>")
        return

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            # Say why rather than failing silently. A staff member typing a
            # command and getting nothing back has no way to tell whether the
            # bot is down, they are unregistered, or the group is wrong.
            logger.info(
                "/reply refused: chat=%s user=%s thread=%s",
                message.chat.id,
                message.from_user.id if message.from_user else None,
                message.message_thread_id,
            )
            await message.reply(
                refusal_reason(message.from_user.id if message.from_user else None)
            )
            return
        _, actor, item = resolved
        if item is None:
            await message.reply(
                "This topic is not linked to a work item. Use /reply inside the "
                "topic of the request you are answering."
            )
            return
        try:
            await relay.send_client_reply(session, gateway(), item, actor, text)
        except Exception as exc:
            logger.exception("/reply failed for work item %s", item.id)
            await message.reply(explain(exc))
            return

    await message.reply("Sent to the client.")


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.reply("Usage: /note <internal note>")
        return

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, actor, item = resolved
        if item is None:
            return
        await relay.add_internal_note(session, gateway(), item, actor, text)


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, _, item = resolved
        if item is None:
            return
        lines = render_history(await load_events(session, item))
        reference = item.display_reference

    body = "\n".join(lines) or "No history recorded."
    await message.reply(f"{reference} — full history\n\n{body}"[:4000])


@router.message(Command("assign"))
async def cmd_assign(message: Message, command: CommandObject) -> None:
    """/assign in reply to a staff member's message, or /assign <telegram id>."""
    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, actor, item = resolved
        if item is None:
            return

        target_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
        elif command.args and command.args.strip().isdigit():
            target_id = int(command.args.strip())

        if target_id is None:
            await message.reply(
                "Reply to the person you want to assign, or use /assign <telegram id>."
            )
            return

        from app.bot.registry import resolve_staff

        assignee = await resolve_staff(session, target_id)
        if assignee is None:
            await message.reply("That user is not registered as active staff.")
            return
        try:
            await relay.assign(session, gateway(), item, assignee, actor)
        except Exception as exc:
            await message.reply(explain(exc))


class StaffCompose(StatesGroup):
    """Composing a message from a button rather than typing a command.

    Deliberately short-lived. State is cleared on send, on cancel, and on any
    sign the person has moved to a different request - a stale draft that
    later attaches itself to the wrong ticket would be worse than no button.
    """

    awaiting_reply = State()
    awaiting_note = State()


async def _client_name(session, item: WorkItem) -> str:
    client = await session.get(Client, item.client_id)
    return client.name if client else "the client"


async def _wrong_topic(message: Message, state: FSMContext, topic_id: int | None) -> bool:
    """Is this reply about the request the draft was started in?

    FSM keys are (chat, user) and carry no topic, so a staff member composing
    in one ticket and then answering a prompt in another would have their text
    attached to the first. In an Operations Group where everyone works several
    topics at once that is not a hypothetical.
    """
    if topic_id is None or message.message_thread_id == topic_id:
        return False
    await state.clear()
    await message.reply(
        "That draft belonged to a different request, so I have discarded it. "
        "Tap 'Reply to client' in this topic to start again."
    )
    return True


@router.message(StaffCompose.awaiting_reply)
async def capture_reply_draft(message: Message, state: FSMContext) -> None:
    """Hold the draft and show it back. Nothing is sent from here."""
    data = await state.get_data()
    if await _wrong_topic(message, state, data.get("topic_id")):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("Type the message the client should see, or tap Cancel.")
        return

    work_item_id = data.get("work_item_id")
    async with session_scope() as session:
        item = await session.get(WorkItem, work_item_id)
        if item is None:
            await state.clear()
            await message.reply("That request no longer exists.")
            return
        client_name = await _client_name(session, item)
        reference = item.display_reference

    await state.update_data(draft=text)
    await message.reply(
        f"This will be sent to {client_name} for {reference}:\n\n{text}\n\n"
        f"Nothing has been sent yet.",
        reply_markup=kb.confirm_reply(work_item_id),
    )


@router.message(StaffCompose.awaiting_note)
async def capture_note_text(message: Message, state: FSMContext) -> None:
    """Internal notes need no preview - nothing leaves the Operations Group."""
    data = await state.get_data()
    if await _wrong_topic(message, state, data.get("topic_id")):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("Type the note, or ignore this to abandon it.")
        return

    await state.clear()
    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            await message.reply(
                refusal_reason(message.from_user.id if message.from_user else None)
            )
            return
        _, actor, item = resolved
        if item is None:
            return
        await relay.add_internal_note(
            session, gateway(), item, actor, text,
            telegram_message_id=message.message_id,
        )


@router.callback_query(F.data.startswith("wi:"))
async def on_action(query: CallbackQuery, state: FSMContext) -> None:
    try:
        action, work_item_id, value = kb.parse_cb(query.data or "")
    except ValueError:
        await query.answer()
        return

    async with session_scope() as session:
        message = query.message
        ctx = await staff_context(
            session, message.chat.id, query.from_user.id if query.from_user else None
        )
        if ctx is None:
            reason = refusal_reason(query.from_user.id if query.from_user else None)
            await query.answer(reason[:190], show_alert=True)
            return
        _, actor = ctx

        item = await session.get(WorkItem, work_item_id)
        if item is None:
            await query.answer("Work item not found.", show_alert=True)
            return

        try:
            note = await _apply(session, query, action, value, item, actor, state)
        except Exception as exc:
            await query.answer(explain(exc)[:190], show_alert=True)
            return

    await query.answer(note or "Done")


async def _apply(
    session, query: CallbackQuery, action, value, item: WorkItem, actor, state: FSMContext
) -> str:
    gw = gateway()

    if action == "claim":
        await relay.claim(session, gw, item, actor)
        await _refresh_keyboard(query, item.id, claimed=True)
        return f"Claimed {item.display_reference}"

    if action == "reply":
        # ForceReply rather than "now type your reply": it quotes the prompt in
        # the composer, so the person can see they are writing to a client and
        # not to the topic. The preview afterwards is the actual safety net.
        await state.set_state(StaffCompose.awaiting_reply)
        await state.update_data(
            work_item_id=item.id, topic_id=query.message.message_thread_id
        )
        name = await _client_name(session, item)
        await query.message.answer(
            f"Reply to {name} for {item.display_reference} - type it below. "
            f"You will see it before it is sent.",
            message_thread_id=query.message.message_thread_id,
            reply_markup=ForceReply(selective=True),
        )
        return "Type your reply"

    if action == "note":
        await state.set_state(StaffCompose.awaiting_note)
        await state.update_data(
            work_item_id=item.id, topic_id=query.message.message_thread_id
        )
        await query.message.answer(
            f"Internal note for {item.display_reference} - type it below. "
            f"This stays in this group.",
            message_thread_id=query.message.message_thread_id,
            reply_markup=ForceReply(selective=True),
        )
        return "Type your note"

    if action == "sendreply":
        data = await state.get_data()
        draft = (data.get("draft") or "").strip()
        if not draft or data.get("work_item_id") != item.id:
            # Covers a second tap on the same preview, and a preview left over
            # from an earlier request. Sending a client the wrong message twice
            # is not a mistake worth being relaxed about.
            await state.clear()
            return "That draft has already been sent or expired"
        await relay.send_client_reply(session, gw, item, actor, draft)
        await state.clear()
        await _seal_preview(query, f"Sent to the client:\n\n{draft}")
        return f"Sent to the client for {item.display_reference}"

    if action == "cancelreply":
        await state.clear()
        await _seal_preview(query, "Cancelled. Nothing was sent to the client.")
        return "Cancelled"

    if action == "reassign":
        # Check the permission before offering the list. Showing someone a menu
        # of colleagues and then refusing every one of them is a worse
        # experience than saying no once.
        actor.require(ROLE_REQUIRED_TO_REASSIGN)

        people = await _assignable(session, item)
        if not people:
            return "No other active staff in this department"
        await query.message.edit_reply_markup(
            reply_markup=kb.assignee_choices(item.id, people)
        )
        return "Choose who to assign it to"

    if action == "setowner":
        assignee = await session.get(Staff, int(value)) if value else None
        if assignee is None or not assignee.is_active:
            return "That person is no longer active staff"
        await relay.assign(session, gw, item, assignee, actor)
        await _refresh_keyboard(query, item.id, claimed=True)
        return f"Assigned to {assignee.display_name}"

    if action == "status":
        await query.message.edit_reply_markup(reply_markup=kb.status_choices(item.id))
        return "Choose a status"

    if action == "priority":
        await query.message.edit_reply_markup(reply_markup=kb.priority_choices(item.id))
        return "Choose a priority"

    if action == "setstatus":
        await relay.change_status(session, gw, item, WorkItemStatus(value), actor)
        await _refresh_keyboard(query, item.id, claimed=item.owner_staff_id is not None)
        return WorkItemStatus(value).label

    if action == "setpriority":
        await relay.change_priority(session, gw, item, Priority(value), actor)
        await _refresh_keyboard(query, item.id, claimed=item.owner_staff_id is not None)
        return Priority(value).label

    if action == "back":
        await _refresh_keyboard(query, item.id, claimed=item.owner_staff_id is not None)
        return ""

    if action == "history":
        lines = render_history(await load_events(session, item))
        await query.message.reply(
            f"{item.display_reference} — full history\n\n" + "\n".join(lines)[:3800]
        )
        return ""

    if action == "close":
        await relay.close(session, gw, item, actor)
        return f"Closed {item.display_reference}"

    return ""


async def _assignable(session, item: WorkItem) -> list[Staff]:
    """Active staff in this department, minus whoever already owns it."""
    from sqlalchemy import select

    result = await session.execute(
        select(Staff)
        .where(
            Staff.is_active.is_(True),
            Staff.department == item.department,
            Staff.id != item.owner_staff_id,
        )
        .order_by(Staff.display_name)
    )
    return list(result.scalars().all())


async def _seal_preview(query: CallbackQuery, text: str) -> None:
    """Replace the preview with its outcome and strip the buttons.

    Leaving a live "Send to client" button under a message that has already
    been sent is an invitation to send it twice.
    """
    try:
        await query.message.edit_text(text[:4000], reply_markup=None)
    except Exception:
        logger.debug("Could not seal preview", exc_info=True)


async def _refresh_keyboard(query: CallbackQuery, work_item_id: int, *, claimed: bool) -> None:
    try:
        await query.message.edit_reply_markup(
            reply_markup=kb.work_item_actions(work_item_id, claimed=claimed)
        )
    except Exception:  # message unchanged, or too old to edit
        logger.debug("Could not refresh keyboard", exc_info=True)


@router.message(F.message_thread_id.is_not(None))
async def topic_message(message: Message) -> None:
    """Anything a staff member posts in a topic.

    Files captioned `/reply ...` go to the client, satisfying PRD 7.5 - staff
    attachments must reach the originating client group. Everything else is
    internal, including a file with no caption. Sending something outward is
    always a deliberate act.
    """
    # This handler is filtered on message_thread_id, which is NOT only set by
    # forum topics: Telegram sets it on any reply in a supergroup, including
    # in client groups with topics switched off. Without the guard below, a
    # client replying to the bot lands here, `_resolve` returns None because
    # this is not an Operations Group, and the handler returns silently -
    # consuming the update before the client router ever sees it. Raising
    # SkipHandler hands it on instead of swallowing it.
    async with session_scope() as session:
        chat = await resolve_chat(session, message.chat.id)
        is_operations = chat is not None and chat.kind is ChatKind.OPERATIONS
    if not is_operations:
        raise SkipHandler

    text = message.text or message.caption or ""
    if text.startswith("/") and not text.startswith("/reply"):
        return  # a command; its own handler deals with it

    attachments = extract_attachments(message)
    if not attachments and not message.text:
        return

    outbound = text.startswith("/reply")
    body = text[len("/reply"):].strip() if outbound else text

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, actor, item = resolved
        if item is None:
            return

        try:
            if outbound and has_attachment(message):
                await relay.send_client_reply(
                    session, gateway(), item, actor,
                    body or "please see the attached.",
                    attachment=attachments[0],
                )
                await message.reply("Sent to the client, with the attachment.")
            elif outbound:
                # `/reply` as plain text is handled by cmd_reply; nothing to do.
                return
            elif attachments:
                await relay.record_internal_attachment(
                    session, gateway(), item, actor, attachments, note=body,
                    telegram_message_id=message.message_id,
                )
            else:
                await relay.add_internal_note(
                    session, gateway(), item, actor, body,
                    telegram_message_id=message.message_id,
                )
        except Exception as exc:
            await message.reply(explain(exc))
