"""Staff handlers, used inside the departmental Operations Groups.

The safety property this file exists to preserve: typing in a topic is
internal. Only `/np_reply` sends to a client. There is no configuration that
changes this and no code path that relays a plain message outward.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot import commands as cmd
from app.bot import keyboards as kb
from app.bot.attachments import extract_attachments, has_attachment
from app.bot.deps import (
    explain,
    gateway,
    prompt_for,
    refusal_reason,
    staff_context,
    work_item_for_thread,
)
from app.bot.registry import resolve_chat
from app.db.base import session_scope
from app.db.models import Client, Staff, WorkItem
from app.domain import work_items as wi
from app.domain.enums import ChatKind, Department, Priority, WorkItemStatus
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


@router.message(cmd.any_case(cmd.REPLY))
async def cmd_reply(message: Message, command: CommandObject) -> None:
    """The only route from an Operations Group to a client."""
    text = (command.args or "").strip()
    if not text:
        await message.reply(f"Usage: /{cmd.REPLY} <message to the client>")
        return

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            # Say why rather than failing silently. A staff member typing a
            # command and getting nothing back has no way to tell whether the
            # bot is down, they are unregistered, or the group is wrong.
            logger.info(
                "%s refused: chat=%s user=%s thread=%s",
                cmd.REPLY,
                message.chat.id,
                message.from_user.id if message.from_user else None,
                message.message_thread_id,
            )
            await message.reply(
                await refusal_reason(
                    message.from_user.id if message.from_user else None,
                    session, message.chat.id,
                )
            )
            return
        _, actor, item = resolved
        if item is None:
            await message.reply(
                f"This topic is not linked to a work item. Use /{cmd.REPLY} inside "
                "the topic of the request you are answering."
            )
            return
        try:
            await relay.send_client_reply(session, gateway(), item, actor, text)
        except Exception as exc:
            logger.exception("%s failed for work item %s", cmd.REPLY, item.id)
            await message.reply(explain(exc))
            return

    await message.reply("Sent to the client.")


@router.message(cmd.any_case(cmd.NOTE))
async def cmd_note(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.reply(f"Usage: /{cmd.NOTE} <internal note>")
        return

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, actor, item = resolved
        if item is None:
            return
        await relay.add_internal_note(session, gateway(), item, actor, text)


@router.message(cmd.any_case(cmd.HISTORY))
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


@router.message(cmd.any_case(cmd.ASSIGN))
async def cmd_assign(message: Message, command: CommandObject) -> None:
    """`/np_assign` in reply to a staff member, or with a telegram id."""
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
                f"Reply to the person you want to assign, or use /{cmd.ASSIGN} <telegram id>."
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


@router.message(cmd.any_case(cmd.LINK))
async def cmd_link(message: Message, command: CommandObject) -> None:
    """`/np_link ACME-1042`, from inside the topic of the other ticket.

    The button covers the common case - something open, in this department,
    raised recently. This covers everything else: an old ticket, a closed one,
    or one belonging to another department. Both end in the same place.
    """
    await _link_by_reference(message, command, remove=False)


@router.message(cmd.any_case(cmd.UNLINK))
async def cmd_unlink(message: Message, command: CommandObject) -> None:
    """`/np_unlink ACME-1042`. The link goes; the events recording it stay."""
    await _link_by_reference(message, command, remove=True)


async def _link_by_reference(message: Message, command: CommandObject, *, remove: bool) -> None:
    name = cmd.UNLINK if remove else cmd.LINK
    raw = (command.args or "").strip()
    if not raw:
        await message.reply(f"Usage: /{name} <reference>, for example /{name} ACME-1042")
        return

    reference = wi.parse_reference(raw)
    if reference is None:
        await message.reply(
            f"{raw!r} does not look like a reference. Any of ACME-SPEX-1042, "
            f"ACME-1042 or 1042 will do."
        )
        return

    async with session_scope() as session:
        resolved = await _resolve(session, message, message.message_thread_id)
        if resolved is None:
            return
        _, actor, item = resolved
        if item is None:
            await message.reply("Send this inside the topic of the ticket you want to link.")
            return

        other = await wi.by_reference(session, reference)
        if other is None:
            await message.reply(f"No ticket found with reference {reference}.")
            return

        try:
            if remove:
                removed = await relay.unlink(session, gateway(), item, other, actor)
                await message.reply(
                    f"Link to {other.display_reference} removed."
                    if removed
                    else f"{item.display_reference} and {other.display_reference} "
                    f"were not linked."
                )
            else:
                await relay.link(session, gateway(), item, other, actor)
                await message.reply(
                    f"{item.display_reference} and {other.display_reference} are now linked."
                )
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
    awaiting_internal = State()
    awaiting_answer = State()


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
        # Offered only where somebody has actually been named. A "tag" button
        # on a group with no contact would either do nothing or need
        # explaining, and both are worse than not showing it.
        from app.bot.registry import leads_for

        source, _ = await relay.chats_for(session, item)
        leads = await leads_for(session, source)

    await state.update_data(draft=text)
    await message.reply(
        f"This will be sent to {client_name} for {reference}:\n\n{text}\n\n"
        f"Nothing has been sent yet.",
        reply_markup=kb.confirm_reply(work_item_id, leads[0] if leads else None),
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
                await refusal_reason(
                    message.from_user.id if message.from_user else None,
                    session, message.chat.id,
                )
            )
            return
        _, actor, item = resolved
        if item is None:
            return
        await relay.add_internal_note(
            session, gateway(), item, actor, text,
            telegram_message_id=message.message_id,
        )


@router.message(StaffCompose.awaiting_internal)
async def capture_internal_request(message: Message, state: FSMContext) -> None:
    """What to ask another department, previewed before it opens anything.

    Nothing here can reach a counterparty, but it still puts a request on
    somebody else's desk - and a half-typed thought landing as a ticket in
    Finance is its own kind of mess.
    """
    data = await state.get_data()
    if await _wrong_topic(message, state, data.get("topic_id")):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("Type what to ask, or ignore this to abandon it.")
        return

    department = Department(data["department"])
    work_item_id = data.get("work_item_id")
    async with session_scope() as session:
        item = await session.get(WorkItem, work_item_id)
        if item is None:
            await state.clear()
            await message.reply("That request no longer exists.")
            return
        reference = item.display_reference

    await state.update_data(draft=text)
    await message.reply(
        f"This will open a new request with {department.label}, linked to "
        f"{reference}:\n\n{text}\n\nNothing has been sent to the client, and "
        f"nothing will be.",
        reply_markup=kb.confirm_internal(work_item_id, department),
    )


@router.message(StaffCompose.awaiting_answer)
async def capture_answer_draft(message: Message, state: FSMContext) -> None:
    """The answer back to the desk that asked, previewed before it is sent.

    Previewed even though it never leaves NexterPay. It is still the thing
    another desk has been waiting on, and it lands in their topic under their
    reference - a half-finished sentence arriving there as "the answer" is a
    smaller mess than one reaching a client, and still a mess.
    """
    data = await state.get_data()
    if await _wrong_topic(message, state, data.get("topic_id")):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("Type the answer, or ignore this to abandon it.")
        return

    work_item_id = data.get("work_item_id")
    async with session_scope() as session:
        item = await session.get(WorkItem, work_item_id)
        if item is None or item.asked_from_id is None:
            await state.clear()
            await message.reply("That request no longer exists.")
            return
        origin = await session.get(WorkItem, item.asked_from_id)
        reference = origin.display_reference if origin else "the desk that asked"

    await state.update_data(draft=text)
    await message.reply(
        f"This goes back to {reference}, in the group that asked:\n\n{text}\n\n"
        f"The client sees nothing of this.",
        reply_markup=kb.confirm_answer(work_item_id, reference),
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
            reason = await refusal_reason(
                query.from_user.id if query.from_user else None,
                session, query.message.chat.id,
            )
            await query.answer(reason[:190], show_alert=True)
            return
        _, actor = ctx

        item = await session.get(WorkItem, work_item_id)
        if item is None:
            await query.answer("Work item not found.", show_alert=True)
            return

        # Answered here, before the work, not after it.
        #
        # Telegram spins a loader on the button until answerCallbackQuery
        # arrives, so answering last means the button spins for as long as the
        # whole action takes. Claiming a request is four API calls - announce,
        # rename the topic, rewrite the header, rebuild the keyboard - each
        # queued behind the rate limiter, and NexterPay's own logs show one
        # callback taking 8.4 seconds. It looked broken because it was
        # unresponsive, which is a different fault from being slow and the one
        # people report.
        #
        # Everything above this line is database reads, in milliseconds, so
        # the two answers that need to be alerts still are. Everything below
        # talks to Telegram.
        await query.answer()

        try:
            await _apply(session, query, action, value, item, actor, state)
        except Exception as exc:
            # The callback is already answered, so this cannot be an alert.
            # Into the topic instead, where it is at least durable - an alert
            # is gone the moment it is dismissed anyway.
            logger.info("%s failed on %s", action, item.display_reference, exc_info=True)
            try:
                await query.message.reply(explain(exc))
            except Exception:
                logger.debug("Could not report the failure", exc_info=True)


async def _apply(
    session, query: CallbackQuery, action, value, item: WorkItem, actor, state: FSMContext
) -> str:
    gw = gateway()

    # Looked up once, here, rather than at each of the eight places the
    # keyboard is rebuilt. If it is missed at even one of them, that button
    # quietly reverts to "Reply to client" - which on this request is the one
    # button that must not exist - and the change is invisible until somebody
    # taps it and writes to a client about a reference the client never saw.
    origin_ref = None
    if item.asked_from_id is not None:
        origin = await session.get(WorkItem, item.asked_from_id)
        origin_ref = origin.display_reference if origin else None

    if action == "claim":
        await relay.claim(session, gw, item, actor)
        await _refresh_keyboard(
            query, item.id, claimed=True, asked_from=origin_ref
        )
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
        text, markup, mode = prompt_for(
            query.from_user,
            f"Reply to {name} for {item.display_reference} - type it below. "
            f"You will see it before it is sent.",
            placeholder=f"Your reply to {name}",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        return "Type your reply"

    if action == "note":
        await state.set_state(StaffCompose.awaiting_note)
        await state.update_data(
            work_item_id=item.id, topic_id=query.message.message_thread_id
        )
        text, markup, mode = prompt_for(
            query.from_user,
            f"Internal note for {item.display_reference} - type it below. "
            f"This stays in this group.",
            placeholder="Internal note",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        return "Type your note"

    if action == "sendreply":
        data = await state.get_data()
        draft = (data.get("draft") or "").strip()
        if not draft or data.get("work_item_id") != item.id:
            # Covers a second tap on the same preview, and a preview left over
            # from an earlier request. Sending a client the wrong message twice
            # is not a mistake worth being relaxed about.
            await state.clear()
            await _say(query, "That draft has already been sent, or it expired.")
            return "That draft has already been sent or expired"
        # "tag" is the second send button, offered only where the group has a
        # named contact. It addresses them by name so they are notified rather
        # than relying on somebody noticing.
        tag = value == "tag"
        await relay.send_client_reply(session, gw, item, actor, draft, tag_lead=tag)
        await state.clear()
        await _seal_preview(query, f"Sent to the client:\n\n{draft}")
        return f"Sent to the client for {item.display_reference}"

    if action == "cancelreply":
        await state.clear()
        await _seal_preview(query, "Cancelled. Nothing was sent to the client.")
        return "Cancelled"

    if action == "answer":
        if item.asked_from_id is None:
            # Not an error and not silence. Somebody has tapped Answer on a
            # request that was raised directly, which is a reasonable thing to
            # try, and the useful reply names the button they actually want.
            await _say(
                query,
                "There is nothing to answer here - this request was raised "
                "directly, not asked by another desk. Use Reply to client.",
            )
            return "Nothing to answer"
        origin = await session.get(WorkItem, item.asked_from_id)
        await state.set_state(StaffCompose.awaiting_answer)
        await state.update_data(
            work_item_id=item.id, topic_id=query.message.message_thread_id
        )
        where = origin.display_reference if origin else "the desk that asked"
        text, markup, mode = prompt_for(
            query.from_user,
            f"Answer {where} - type it below. It goes back to the desk that "
            f"asked, not to the client. You will see it before it is sent.",
            placeholder=f"Your answer for {where}",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        return f"Type your answer for {where}"

    if action == "sendanswer":
        data = await state.get_data()
        draft = (data.get("draft") or "").strip()
        if not draft or data.get("work_item_id") != item.id:
            await state.clear()
            await _say(query, "That draft has already been sent, or it expired.")
            return "That draft has already been sent or expired"
        origin = await relay.answer_internal(session, gw, item, actor, draft)
        await state.clear()
        if origin is None:
            await _seal_preview(
                query, "Could not answer - the request that asked is gone."
            )
            return "Nothing to answer"
        await _seal_preview(
            query, f"Sent to {origin.display_reference}:\n\n{draft}"
        )
        return f"Answered {origin.display_reference}"

    if action == "cancelanswer":
        await state.clear()
        await _seal_preview(query, "Cancelled. No answer was sent.")
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
            reply_markup=kb.assignee_choices(item.id, people, item.department)
        )
        return "Choose who to assign it to"

    if action == "file":
        options = await _codeable(session, item)
        if not options:
            return "No counterparties have codes yet - set one with /np_setcode"
        await query.message.edit_reply_markup(
            reply_markup=kb.supplier_choices(item.id, options)
        )
        return "Which supplier is this about?"

    if action == "link":
        linked = await wi.linked_to(session, item)
        candidates = await _linkable(session, item, linked)
        if not candidates and not linked:
            return (
                "Nothing else to link to yet. Use /np_link with a reference for "
                "an older or closed ticket."
            )
        await query.message.edit_reply_markup(
            reply_markup=kb.link_choices(item.id, candidates, linked)
        )
        return "Linked tickets carry a cross; tap one below to connect it"

    if action == "dolink":
        other = await session.get(WorkItem, int(value)) if value else None
        if other is None:
            return "That ticket no longer exists"
        await relay.link(session, gw, item, other, actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return f"Linked to {other.display_reference}"

    if action == "unlink":
        other = await session.get(WorkItem, int(value)) if value else None
        if other is None:
            return "That ticket no longer exists"
        removed = await relay.unlink(session, gw, item, other, actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return (
            f"Link to {other.display_reference} removed" if removed else "They were not linked"
        )

    if action == "askdept":
        others = [d for d in Department if d is not item.department]
        await query.message.edit_reply_markup(
            reply_markup=kb.department_choices(item.id, others)
        )
        return "Which department should look at this?"

    if action == "setdept":
        department = Department(value)
        await state.set_state(StaffCompose.awaiting_internal)
        await state.update_data(
            work_item_id=item.id,
            topic_id=query.message.message_thread_id,
            department=department.value,
        )
        text, markup, mode = prompt_for(
            query.from_user,
            f"What should {department.label} look at, on {item.display_reference}? "
            f"Type it below. Nothing reaches the client.",
            placeholder=f"What to ask {department.label}",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        return f"Type what to ask {department.label}"

    if action == "sendinternal":
        data = await state.get_data()
        draft = (data.get("draft") or "").strip()
        if not draft or data.get("work_item_id") != item.id:
            await state.clear()
            await _say(query, "That draft has already been sent, or it expired.")
            return "That draft has already been sent or expired"
        department = Department(value)
        subject = (draft.splitlines()[0] if draft else "")[:120] or "Internal request"
        opened = await relay.open_internal(
            session, gw, origin=item, department=department,
            subject=subject, body=draft, actor=actor,
            # A callable, not a ready-made keyboard. It used to pass
            # work_item_actions(0, ...) - the id does not exist until the row
            # does - so every button on every request opened this way pointed
            # at work item zero and did nothing at all.
            keyboard_for=lambda new_id: kb.work_item_actions(
                new_id, claimed=False, asked_from=item.display_reference
            ),
        )
        await state.clear()
        await _seal_preview(
            query, f"Asked {department.label}. Opened {opened.display_reference}."
        )
        return f"Opened {opened.display_reference} with {department.label}"

    if action == "cancelinternal":
        await state.clear()
        await _seal_preview(query, "Cancelled. No request was opened.")
        return "Cancelled"

    if action == "setsupplier":
        supplier = await session.get(Client, int(value)) if value else None
        if supplier is None:
            return "That counterparty no longer exists"
        await relay.file_under(session, gw, item, supplier, actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return f"Filed under {supplier.code}"

    if action == "setowner":
        assignee = await session.get(Staff, int(value)) if value else None
        if assignee is None or not assignee.is_active:
            return "That person is no longer active staff"
        await relay.assign(session, gw, item, assignee, actor)
        await _refresh_keyboard(
            query, item.id, claimed=True, asked_from=origin_ref
        )
        return f"Assigned to {assignee.display_name}"

    if action == "status":
        await query.message.edit_reply_markup(reply_markup=kb.status_choices(item.id))
        return "Choose a status"

    if action == "priority":
        await query.message.edit_reply_markup(reply_markup=kb.priority_choices(item.id))
        return "Choose a priority"

    if action == "setstatus":
        await relay.change_status(session, gw, item, WorkItemStatus(value), actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return WorkItemStatus(value).label

    if action == "setpriority":
        await relay.change_priority(session, gw, item, Priority(value), actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return Priority(value).label

    if action in ("more", "less", "back"):
        # "back" returns from a chooser - Status, Priority, an assignee list -
        # and those are all reached from the expanded set, so it opens back
        # into it rather than collapsing underneath the person.
        await _refresh_keyboard(
            query, item.id,
            claimed=item.owner_staff_id is not None,
            expanded=action in ("more", "back"),
        )
        return ""

    if action == "history":
        lines = render_history(await load_events(session, item))
        await query.message.reply(
            f"{item.display_reference} — full history\n\n" + "\n".join(lines)[:3800]
        )
        return ""

    if action == "reopen":
        # Manager and above, enforced in the domain. Checked there rather than
        # here so the rule holds however it is reached.
        await relay.reopen(session, gw, item, actor)
        await _refresh_keyboard(
            query, item.id, claimed=item.owner_staff_id is not None, asked_from=origin_ref
        )
        return f"Reopened {item.display_reference}"

    if action == "close":
        await relay.close(session, gw, item, actor)
        try:
            await query.message.edit_reply_markup(reply_markup=kb.closed_actions(item.id))
        except Exception:
            logger.debug("Could not swap in the closed keyboard", exc_info=True)
        return f"Closed {item.display_reference}"

    return ""


async def _codeable(session, item: WorkItem) -> list[Client]:
    """Counterparties that can be filed against - those with a code.

    The client the request came from is excluded: filing a ticket under its
    own client is not a supplier relationship, it is a mistake.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(Client)
        .where(
            Client.is_active.is_(True),
            Client.code.is_not(None),
            Client.id != item.client_id,
        )
        .order_by(Client.code)
    )
    return list(result.scalars().all())


async def _linkable(session, item: WorkItem, already_linked: list[WorkItem]) -> list[WorkItem]:
    """Tickets offered as link candidates on the keyboard.

    Open ones in the same Operations Group, most recently touched first, and
    capped. This is the quick path, not the complete one: a desk with forty
    open tickets cannot pick from forty buttons, and the ticket someone
    actually wants is usually one they were looking at minutes ago. Anything
    older, closed, or in another department goes through /np_link with a
    reference, which has no such limit.
    """
    from sqlalchemy import select

    excluded = {item.id} | {other.id for other in already_linked}
    result = await session.execute(
        select(WorkItem)
        .where(
            WorkItem.operations_chat_id == item.operations_chat_id,
            WorkItem.status != WorkItemStatus.CLOSED,
            WorkItem.id.not_in(excluded),
        )
        .order_by(WorkItem.updated_at.desc())
        .limit(8)
    )
    return list(result.scalars().all())


async def _assignable(session, item: WorkItem) -> list[Staff]:
    """Active staff on this desk, minus whoever already owns it.

    Joined through memberships rather than read off the person, so someone who
    works both Support and Compliance appears in both lists - which is the
    whole point of letting them span two.
    """
    from sqlalchemy import select

    from app.db.models import StaffDepartment

    result = await session.execute(
        select(Staff)
        .join(StaffDepartment, StaffDepartment.staff_id == Staff.id)
        .where(
            Staff.is_active.is_(True),
            StaffDepartment.department == item.department,
            Staff.id != item.owner_staff_id,
        )
        .order_by(Staff.display_name)
    )
    return list(result.scalars().all())


async def _say(query: CallbackQuery, text: str) -> None:
    """Tell the person something, in the topic.

    Needed because the callback is now answered before the work runs, so a
    string returned from `_apply` reaches nobody. Every branch whose entire
    output was that string has to say it out loud instead - which is the same
    lesson as the silent administrator commands, arriving from a different
    direction.
    """
    try:
        await query.message.reply(text)
    except Exception:
        logger.debug("Could not reply in the topic", exc_info=True)


async def _seal_preview(query: CallbackQuery, text: str) -> None:
    """Replace the preview with its outcome and strip the buttons.

    Leaving a live "Send to client" button under a message that has already
    been sent is an invitation to send it twice.
    """
    try:
        await query.message.edit_text(text[:4000], reply_markup=None)
    except Exception:
        logger.debug("Could not seal preview", exc_info=True)


async def _refresh_keyboard(
    query: CallbackQuery, work_item_id: int, *, claimed: bool, expanded: bool = False,
    asked_from: str | None = None,
) -> None:
    """Rebuild the buttons after an action.

    `asked_from` has to be carried through here as well as at creation. It is
    what keeps the Answer button on an asked-for request: without it, the
    first tap on Claim or More would quietly rebuild the row with "Reply to
    client" in the middle - the button that must not be on this request at
    all - and nobody would see it change.
    """
    try:
        await query.message.edit_reply_markup(
            reply_markup=kb.work_item_actions(
                work_item_id, claimed=claimed, expanded=expanded,
                asked_from=asked_from,
            )
        )
    except Exception:  # message unchanged, or too old to edit
        logger.debug("Could not refresh keyboard", exc_info=True)


@router.message(F.message_thread_id.is_not(None))
async def topic_message(message: Message) -> None:
    """Anything a staff member posts in a topic.

    Files captioned `/np_reply ...` go to the client, satisfying PRD 7.5 - staff
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
    reply_cmd = f"/{cmd.REPLY}"
    if text.startswith("/") and not text.startswith(reply_cmd):
        return  # a command; its own handler deals with it

    attachments = extract_attachments(message)
    if not attachments and not message.text:
        return

    outbound = text.startswith(reply_cmd)
    body = text[len(reply_cmd):].strip() if outbound else text

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
                # `/np_reply` as plain text is handled by cmd_reply; nothing to do.
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
