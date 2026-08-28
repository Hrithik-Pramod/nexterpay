"""The relay: everything that moves between a client group and a topic.

Safety rule, and the reason this module is small and explicit: **only
`send_client_reply` ever writes to a client chat.** Internal notes and staff
discussion have no path outward, by construction rather than by convention.
`tests/test_relay.py` asserts this directly.

Two other things happen here by design:

* Every state change is announced into the topic as a visible line, because
  NexterPay review history by reading the group rather than through any
  interface.
* Every outbound message to a client is recorded as a `Message`, because a
  client reply pointing at it is how the reply-to-acknowledgement routing
  resolves the work item. An unrecorded outbound message is an anchor the
  client can reply to and we cannot match.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Attachment, Chat, Client, Event, Message, Staff, WorkItem
from app.domain import work_items as wi
from app.domain.enums import EventType, MessageDirection, Priority, WorkItemStatus
from app.domain.history import render_event
from app.domain.work_items import Actor
from app.services.gateway import TelegramGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingAttachment:
    file_id: str
    file_unique_id: str
    kind: str
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


async def chats_for(session: AsyncSession, item: WorkItem) -> tuple[Chat, Chat]:
    """(client group, operations group) for a work item, loaded explicitly."""
    source = await session.get(Chat, item.source_chat_id)
    ops = await session.get(Chat, item.operations_chat_id)
    if source is None or ops is None:
        raise LookupError(f"Chats missing for work item {item.id}")
    return source, ops


async def _record_message(
    session: AsyncSession,
    item: WorkItem,
    *,
    direction: MessageDirection,
    chat_id: int,
    message_id: int | None,
    sender_name: str,
    text: str | None,
    sender_telegram_user_id: int | None = None,
) -> Message:
    message = Message(
        work_item_id=item.id,
        direction=direction,
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        sender_name=sender_name,
        sender_telegram_user_id=sender_telegram_user_id,
        text=text,
    )
    session.add(message)
    await session.flush()
    return message


async def announce(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, event: Event
) -> None:
    """Post an event into the topic so it is visible, not merely recorded."""
    if item.topic_id is None:
        logger.debug("No topic yet for %s; skipping announcement", item.display_reference)
        return
    _, ops = await chats_for(session, item)
    await gateway.send_message(
        ops.telegram_chat_id,
        f"• {render_event(event)}",
        thread_id=item.topic_id,
    )


def topic_name(item: WorkItem, client_name: str) -> str:
    return f"{item.display_reference} · {client_name} · {item.subject}"[:128]


def header_text(item: WorkItem, client_name: str, owner_name: str | None = None) -> str:
    """The live summary at the top of the topic.

    Edited in place whenever ownership, status or priority changes. PRD 7.3
    requires ownership to be clearly visible to everyone in the Operations
    Group; a header frozen at "unassigned" would not satisfy that.
    """
    return (
        f"{item.display_reference} — {item.subject}\n"
        f"Client: {client_name}\n"
        f"Raised by: {item.raised_by_name}\n"
        f"Department: {item.department.value.title()}\n"
        f"Status: {item.status.label}   Priority: {item.priority.label}\n"
        f"Owner: {owner_name or 'unassigned'}"
    )


async def refresh_header(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem
) -> None:
    """Rewrite the topic header to match the work item's current state."""
    if item.header_message_id is None:
        return
    _, ops = await chats_for(session, item)
    client = await session.get(Client, item.client_id)
    owner = await session.get(Staff, item.owner_staff_id) if item.owner_staff_id else None

    try:
        await gateway.edit_message_text(
            ops.telegram_chat_id,
            item.header_message_id,
            header_text(
                item,
                client.name if client else "Unknown client",
                owner.display_name if owner else None,
            ),
        )
    except Exception:
        # An unchanged message, or one too old to edit. The announcements in
        # the topic still carry the change, so this is cosmetic.
        logger.debug("Could not refresh header for %s", item.display_reference, exc_info=True)


def acknowledgement_text(item: WorkItem) -> str:
    return (
        f"Request {item.display_reference} has been logged with our "
        f"{item.department.value.title()} team.\n\n"
        f"Please reply to this message to add anything further to it."
    )


async def open_request(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    source_chat: Chat,
    subject: str,
    body: str,
    raised_by_name: str,
    raised_by_telegram_user_id: int | None = None,
    attachments: list[IncomingAttachment] | None = None,
    keyboard=None,
) -> WorkItem:
    """A client request becomes a work item, a topic, and an acknowledgement.

    Order matters: the acknowledgement is sent last and recorded, because it is
    the anchor every later client reply will point at.
    """
    item = await wi.create_work_item(
        session,
        source_chat=source_chat,
        subject=subject,
        original_message=body,
        raised_by_name=raised_by_name,
        raised_by_telegram_user_id=raised_by_telegram_user_id,
    )
    client = await session.get(Client, item.client_id)
    client_name = client.name if client else "Unknown client"
    _, ops = await chats_for(session, item)

    thread_id = await gateway.create_topic(ops.telegram_chat_id, topic_name(item, client_name))
    await wi.attach_topic(session, item, thread_id)

    header = await gateway.send_message(
        ops.telegram_chat_id,
        header_text(item, client_name),
        thread_id=thread_id,
        reply_markup=keyboard,
    )
    item.header_message_id = header.message_id
    await _record_message(
        session, item,
        direction=MessageDirection.INTERNAL,
        chat_id=ops.telegram_chat_id,
        message_id=header.message_id,
        sender_name="NexterPay Operations",
        text=header_text(item, client_name),
    )

    await gateway.send_message(
        ops.telegram_chat_id,
        f"{item.raised_by_name}:\n{body}",
        thread_id=thread_id,
    )

    for att in attachments or []:
        sent = await gateway.send_file(
            ops.telegram_chat_id, att.file_id, att.kind, thread_id=thread_id
        )
        await _store_attachment(session, item, att, ops.telegram_chat_id, sent.message_id,
                                MessageDirection.INBOUND, item.raised_by_name)

    ack = await gateway.send_message(
        source_chat.telegram_chat_id, acknowledgement_text(item)
    )
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=source_chat.telegram_chat_id,
        message_id=ack.message_id,
        sender_name="NexterPay Operations",
        text=acknowledgement_text(item),
    )
    return item


async def _store_attachment(
    session: AsyncSession,
    item: WorkItem,
    att: IncomingAttachment,
    chat_id: int,
    message_id: int | None,
    direction: MessageDirection,
    sender_name: str,
) -> None:
    message = await _record_message(
        session, item,
        direction=direction,
        chat_id=chat_id,
        message_id=message_id,
        sender_name=sender_name,
        text=att.file_name,
    )
    session.add(
        Attachment(
            work_item_id=item.id,
            message_id=message.id,
            file_id=att.file_id,
            file_unique_id=att.file_unique_id,
            file_name=att.file_name,
            mime_type=att.mime_type,
            file_size=att.file_size,
            kind=att.kind,
        )
    )
    await session.flush()
    event = await wi.record_event(
        session, item, EventType.ATTACHMENT_RECEIVED,
        Actor(name=sender_name),
        kind=att.kind, file_name=att.file_name,
    )
    return event


async def relay_client_message(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    *,
    text: str | None,
    sender_name: str,
    telegram_message_id: int,
    sender_telegram_user_id: int | None = None,
    attachments: list[IncomingAttachment] | None = None,
) -> None:
    """Client → topic. Reopens nothing and changes no status; staff decide."""
    source, ops = await chats_for(session, item)

    await _record_message(
        session, item,
        direction=MessageDirection.INBOUND,
        chat_id=source.telegram_chat_id,
        message_id=telegram_message_id,
        sender_name=sender_name,
        sender_telegram_user_id=sender_telegram_user_id,
        text=text,
    )
    event = await wi.record_event(
        session, item, EventType.CLIENT_MESSAGE_RECEIVED,
        Actor(name=sender_name, telegram_user_id=sender_telegram_user_id),
        text=(text or "")[:500],
    )

    # NexterPay asked for the owner to be pinged when a client chases, so the
    # message is not merely present in the topic but actually lands on the
    # person responsible. Unowned items have nobody to ping, and fall back to
    # the plain form.
    owner = (
        await session.get(Staff, item.owner_staff_id)
        if item.owner_staff_id is not None
        else None
    )

    if text:
        if owner is not None:
            body = (
                f"{mention_for(owner)} — {html.escape(sender_name)} has replied "
                f"on {item.display_reference}:\n{html.escape(text)}"
            )
            await gateway.send_message(
                ops.telegram_chat_id, body, thread_id=item.topic_id, parse_mode="HTML"
            )
        else:
            await gateway.send_message(
                ops.telegram_chat_id,
                f"{sender_name} (client):\n{text}",
                thread_id=item.topic_id,
            )
    elif owner is not None and attachments:
        # An attachment with no words still needs the owner to know.
        await gateway.send_message(
            ops.telegram_chat_id,
            f"{mention_for(owner)} — {html.escape(sender_name)} has sent an "
            f"attachment on {item.display_reference}.",
            thread_id=item.topic_id,
            parse_mode="HTML",
        )

    for att in attachments or []:
        sent = await gateway.send_file(
            ops.telegram_chat_id, att.file_id, att.kind,
            thread_id=item.topic_id, caption=f"From {sender_name}",
        )
        await _store_attachment(session, item, att, ops.telegram_chat_id, sent.message_id,
                                MessageDirection.INBOUND, sender_name)

    await announce(session, gateway, item, event)


async def send_client_reply(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    actor: Actor,
    text: str,
    *,
    attachment: IncomingAttachment | None = None,
) -> None:
    """The only path from NexterPay to a client.

    The reply carries the reference and becomes the new anchor, so replying to
    it resolves back to this work item.
    """
    actor.require_any()
    source, ops = await chats_for(session, item)
    outbound = f"{item.display_reference} — {text}"

    sent = await gateway.send_message(source.telegram_chat_id, outbound)
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=source.telegram_chat_id,
        message_id=sent.message_id,
        sender_name=actor.name,
        text=outbound,
    )

    if attachment is not None:
        file_msg = await gateway.send_file(
            source.telegram_chat_id, attachment.file_id, attachment.kind
        )
        await _store_attachment(session, item, attachment, source.telegram_chat_id,
                                file_msg.message_id, MessageDirection.OUTBOUND, actor.name)

    event = await wi.record_event(
        session, item, EventType.STAFF_REPLY_SENT, actor, text=outbound[:500]
    )
    await announce(session, gateway, item, event)


async def add_internal_note(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    actor: Actor,
    text: str,
    *,
    telegram_message_id: int | None = None,
) -> None:
    """Internal only. Touches no client chat - see the module docstring."""
    _, ops = await chats_for(session, item)
    await _record_message(
        session, item,
        direction=MessageDirection.INTERNAL,
        chat_id=ops.telegram_chat_id,
        message_id=telegram_message_id,
        sender_name=actor.name,
        text=text,
    )
    event = await wi.record_event(
        session, item, EventType.INTERNAL_NOTE_ADDED, actor, note=text[:500]
    )
    await announce(session, gateway, item, event)


async def record_internal_attachment(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    actor: Actor,
    attachments: list[IncomingAttachment],
    *,
    note: str = "",
    telegram_message_id: int | None = None,
) -> None:
    """A file a staff member posted in the topic, kept internally.

    Already visible in the topic - Telegram put it there. We record it so it
    forms part of the work item, and it goes nowhere near the client.
    """
    _, ops = await chats_for(session, item)
    for att in attachments:
        await _store_attachment(
            session, item, att, ops.telegram_chat_id, telegram_message_id,
            MessageDirection.INTERNAL, actor.name,
        )
        telegram_message_id = None  # only the first record owns the real id
    if note:
        await add_internal_note(session, gateway, item, actor, note)


def mention_for(staff) -> str:
    """A real Telegram mention, so the person is notified rather than named.

    Requires parse_mode="HTML" at the call site. Falls back to the plain name
    when we have no Telegram id, which is better than a dead link.
    """
    name = html.escape(staff.display_name)
    if staff.telegram_user_id:
        return f'<a href="tg://user?id={staff.telegram_user_id}">{name}</a>'
    return name


async def notify_owner(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, assignee
) -> None:
    """Tell someone a work item is now theirs (PRD 3.6).

    A mention in the topic rather than a direct message: a bot cannot start a
    private conversation with someone who has never messaged it, so a DM would
    silently fail for exactly the staff who most need telling. Who else should
    be notified, and on which events, is still open with NexterPay.
    """
    if item.topic_id is None:
        return
    _, ops = await chats_for(session, item)
    await gateway.send_message(
        ops.telegram_chat_id,
        f"{mention_for(assignee)} — {item.display_reference} is now assigned to you.",
        thread_id=item.topic_id,
        # Without this the link was sent as literal text: the owner saw raw
        # HTML and was never actually pinged. The whole point of the message
        # is the notification.
        parse_mode="HTML",
    )


async def change_status(
    session: AsyncSession, gateway: TelegramGateway,
    item: WorkItem, status: WorkItemStatus, actor: Actor,
) -> None:
    before = item.status
    await wi.change_status(session, item, status, actor)
    if item.status is not before:
        await announce(session, gateway, item, await _latest_event(session, item))
        await refresh_header(session, gateway, item)


async def change_priority(
    session: AsyncSession, gateway: TelegramGateway,
    item: WorkItem, priority: Priority, actor: Actor,
) -> None:
    before = item.priority
    await wi.change_priority(session, item, priority, actor)
    if item.priority is not before:
        await announce(session, gateway, item, await _latest_event(session, item))
        await refresh_header(session, gateway, item)


async def claim(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, actor: Actor
) -> None:
    before = await _last_event_id(session, item)
    await wi.claim(session, item, actor)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)


async def assign(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, assignee, actor: Actor
) -> None:
    before = await _last_event_id(session, item)
    await wi.assign(session, item, assignee, actor)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)
    if assignee.id != (actor.staff.id if actor.staff else None):
        await notify_owner(session, gateway, item, assignee)


async def close(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, actor: Actor,
    *, notify_client: bool = True,
) -> None:
    """Close the work item, announce it, and archive the topic.

    Whether the client is told is a setting rather than a hard rule - it is
    pre-start question A2 and NexterPay have not decided yet.
    """
    if item.status is WorkItemStatus.CLOSED:
        # Second tap on a Close button that is still on screen. wi.close() is
        # already idempotent, but everything after it was not: the client was
        # sent a second "your request has been closed" message before Telegram
        # rejected the duplicate topic close. Telling a customer twice that
        # their case is shut is worse than the crash that revealed it.
        logger.info("%s is already closed; ignoring", item.display_reference)
        return

    _, ops = await chats_for(session, item)
    before = await _last_event_id(session, item)
    await wi.close(session, item, actor)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)

    if notify_client:
        await send_client_reply(
            session, gateway, item, actor,
            "this request has now been completed and closed. "
            "Please reply here if anything remains outstanding.",
        )

    if item.topic_id is not None:
        await gateway.close_topic(ops.telegram_chat_id, item.topic_id)
        event = await wi.record_event(session, item, EventType.TOPIC_CLOSED, actor)
        logger.info("Closed topic %s for %s", item.topic_id, item.display_reference)
        del event


async def _last_event_id(session: AsyncSession, item: WorkItem) -> int:
    from sqlalchemy import select

    result = await session.execute(
        select(Event.id).where(Event.work_item_id == item.id).order_by(Event.id.desc()).limit(1)
    )
    return result.scalar_one_or_none() or 0


async def _announce_since(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, after_id: int
) -> None:
    """Announce every event a domain call produced, not merely the last one.

    A single action can record more than one fact - claiming records both the
    ownership change and the status change it triggers. Announcing only the
    latest event silently dropped "Claimed by ..." from the topic, which is
    exactly the ownership visibility PRD 7.3 asks for.
    """
    from sqlalchemy import select

    result = await session.execute(
        select(Event)
        .where(Event.work_item_id == item.id, Event.id > after_id)
        .order_by(Event.id)
    )
    for event in result.scalars().all():
        await announce(session, gateway, item, event)


async def _latest_event(session: AsyncSession, item: WorkItem) -> Event:
    from sqlalchemy import select

    result = await session.execute(
        select(Event)
        .where(Event.work_item_id == item.id)
        .order_by(Event.id.desc())
        .limit(1)
    )
    return result.scalar_one()
