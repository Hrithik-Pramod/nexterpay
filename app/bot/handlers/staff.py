"""Staff handlers, used inside the departmental Operations Groups.

The safety property this file exists to preserve: typing in a topic is
internal. Only `/reply` sends to a client. There is no configuration that
changes this and no code path that relays a plain message outward.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot.attachments import extract_attachments, has_attachment
from app.bot.deps import (
    explain,
    gateway,
    refusal_reason,
    staff_context,
    work_item_for_thread,
)
from app.db.base import session_scope
from app.db.models import WorkItem
from app.domain.enums import Priority, WorkItemStatus
from app.domain.history import load_events, render_history
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


@router.callback_query(F.data.startswith("wi:"))
async def on_action(query: CallbackQuery) -> None:
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
            note = await _apply(session, query, action, value, item, actor)
        except Exception as exc:
            await query.answer(explain(exc)[:190], show_alert=True)
            return

    await query.answer(note or "Done")


async def _apply(session, query: CallbackQuery, action, value, item: WorkItem, actor) -> str:
    gw = gateway()

    if action == "claim":
        await relay.claim(session, gw, item, actor)
        await _refresh_keyboard(query, item.id, claimed=True)
        return f"Claimed {item.display_reference}"

    if action == "reassign":
        await query.message.reply(
            "Reply to the person you want to assign and send /assign."
        )
        return "Use /assign"

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
