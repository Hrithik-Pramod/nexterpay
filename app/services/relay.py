"""The relay: everything that moves between a client group and a topic.

Safety rule, and the reason this module is small and explicit: **the only
route by which anything a member of staff wrote reaches a client is
`send_client_reply`.** Internal notes and staff discussion have no path
outward, by construction rather than by convention. `tests/test_relay.py`
asserts this directly.

A short, named set of functions also writes to a client chat, but only ever
with text this module composes itself: the acknowledgement in `open_request`,
the opening message in `open_outbound`, the anchor in `post_anchor`, the
closure notice in `close`, and the note in `relay_client_message` telling
someone a request is already closed. Nothing in that list can carry staff
wording. `test_only_these_functions_may_write_to_a_client_chat` holds the same
list and fails if a sixth appears.

`link` is deliberately not on it and must never join it. A link is an internal
observation that two pieces of work are the same problem, and the reference it
names can belong to another client or carry a supplier code - neither of which
the client whose topic it appears in is entitled to see.

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

from app.db.base import utcnow
from app.db.models import Attachment, Chat, Client, Event, Message, Staff, WorkItem
from app.domain import work_items as wi
from app.domain.enums import (
    Department,
    EventType,
    MessageDirection,
    Priority,
    WorkItemStatus,
)
from app.domain.errors import DomainError
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


# The traffic light, on the front of every topic title.
#
# NexterPay asked for red / amber / green in the topic list. Telegram fixes a
# topic's colour when it is created and `editForumTopic` will not change it -
# only the name and the icon can move. So the light goes on the name, where it
# also has the advantage of sitting next to the reference rather than being a
# dot on its own.
#
# The list truncates from the right, so a leading character is the one thing
# that is never cut off.
LIGHT_UNCLAIMED = "🔴"
LIGHT_WORKING = "🟠"
LIGHT_DONE = "🟢"


# Urgent priority, marked rather than coloured.
#
# NexterPay asked for High priority in red font. Telegram has no font colour:
# a message can be bold, italic, underlined, struck through, hidden behind a
# spoiler, monospaced or quoted, and that is the whole list. So the emphasis
# has to be a character.
#
# Deliberately not a red circle. Red already means "nobody has picked this up"
# in the topic list, and the same colour meaning two things is worse than no
# colour at all. An exclamation reads as urgent without borrowing anything.
PRIORITY_MARKS = {Priority.CRITICAL: "‼️", Priority.HIGH: "❗"}


def priority_text(item: WorkItem) -> str:
    """The priority, with a mark on the two that need one."""
    mark = PRIORITY_MARKS.get(item.priority)
    return f"{mark} {item.priority.label}" if mark else item.priority.label


def traffic_light(item: WorkItem) -> str:
    """Red until someone takes it, amber while it moves, green once closed.

    Completed is deliberately amber. NexterPay were asked directly whether
    work-finished-but-not-archived should count as green and said no: green
    means closed, and nothing else.
    """
    if item.status is WorkItemStatus.CLOSED:
        return LIGHT_DONE
    if item.status is WorkItemStatus.OPEN and item.owner_staff_id is None:
        return LIGHT_UNCLAIMED
    return LIGHT_WORKING


def topic_name(item: WorkItem, client_name: str) -> str:
    """What the topic is called in the list, which is where triage happens.

    The light and the priority mark both go here, and for the same reason.
    NexterPay asked for High to stand out; it was built into the header only,
    where you have to open a request to see it. A priority you cannot see
    while scanning is a priority you cannot sort by, which leaves it doing
    nothing that the header's own status line was not already doing.

    Mark after the light, not before: the light answers "is anyone on this",
    which is the first question, and every topic has one. Only two priorities
    in five carry a mark, so leading with it would ragged the list.
    """
    # No mark once it is closed. Green says finished and the mark says drop
    # everything, and a list of archived work carrying urgency flags trains
    # people to read past both. Urgency is a claim about what to do next, and
    # there is nothing next.
    mark = None if item.status is WorkItemStatus.CLOSED else PRIORITY_MARKS.get(item.priority)

    # The counterparty's name is deliberately absent. NexterPay's point, on
    # 5 September: the four-letter code is already in the reference, so
    # "ACME-1036 · Acme Payments · ..." says Acme twice and spends fifteen
    # characters doing it. Telegram truncates a topic name at 128 and the list
    # cuts from the right, so those characters come straight out of the
    # subject - the only part that says what the request is actually about.
    #
    # `client_name` is kept in the signature: it is what the caller has to
    # hand, and dropping it would make restoring this a change at every call
    # site rather than a change here.
    return (
        f"{traffic_light(item)}{mark or ''} {item.display_reference} · "
        f"{item.subject}"
    )[:128]


def _mention(name: str, telegram_user_id: int | None) -> str:
    """A tappable name, where we know who they are.

    NexterPay asked for the people in the header to be mentions rather than
    text, and they were right: a name you can tap is a person you can reach,
    and the header is where somebody looks when they need the owner rather
    than the request. Falls back to the plain name when we have no id, which
    is better than a dead link.
    """
    escaped = html.escape(name)
    if telegram_user_id:
        return f'<a href="tg://user?id={telegram_user_id}">{escaped}</a>'
    return escaped


def header_text(
    item: WorkItem,
    client_name: str,
    owner_name: str | None = None,
    linked_references: list[str] | None = None,
    owner_telegram_user_id: int | None = None,
    leads: list | None = None,
) -> str:
    """The live summary at the top of the topic.

    Edited in place whenever ownership, status or priority changes. PRD 7.3
    requires ownership to be clearly visible to everyone in the Operations
    Group; a header frozen at "unassigned" would not satisfy that.

    Direction is spelled out on both sides rather than marking only the
    outbound ones. The topic carries a "Raised by X with Y" line at the very
    top, but that scrolls away within a few messages while the header stays
    pinned - and a name on its own does not say which way the request runs.
    Someone opening a topic cold needs to know whether they are chasing this
    counterparty or answering them, before they read a word of the thread.
    """
    e = html.escape
    direction = "we raised this" if item.raised_by_us else "they raised this"

    # What the counterparty actually said, leading, in their own words.
    #
    # NexterPay's point, and a fair one: a block of fields in a single weight
    # reads as a form, and the one thing you need - what they asked for - is
    # the easiest part to skim past. So it comes first and in quotes, the
    # labels are bold, and the values are not.
    original = " ".join((item.original_message or "").split())
    if len(original) > 300:
        original = original[:299].rstrip() + "…"

    raised = item.created_at.strftime("%d %b %Y") if item.created_at else "unknown date"

    lines = [
        f"<b>{e(item.display_reference)}</b> — {e(item.subject)}",
    ]
    if original:
        lines.append(f"<i>“{e(original)}”</i>")
    lines += [
        "",
        f"<b>Raised</b>  {raised} by "
        f"{_mention(item.raised_by_name, item.raised_by_telegram_user_id)} "
        f"({direction})",
        f"<b>Client</b>  {e(client_name)}",
        f"<b>Department</b>  {e(item.department.label)}",
        f"<b>Status</b>  {e(item.status.label)}    "
        f"<b>Priority</b>  {e(priority_text(item))}",
        f"<b>Owner</b>  "
        f"{_mention(owner_name, owner_telegram_user_id) if owner_name else 'unassigned'}",
    ]
    # Who to address on the other side.
    #
    # NexterPay asked, on 5 September: "once leads are set, in our operations
    # groups, how do we look up the lead name?" There was no answer - the only
    # way to find out was to go into the counterparty's own group and run
    # /npleads there, which is exactly the trip the header exists to save.
    #
    # Tappable, so it is a person you can reach rather than a name you have to
    # go and find. Absent entirely when nobody has been named: a permanent
    # "Contact: none" is a line of noise on every header to save a moment's
    # thought on a few, which is the same reasoning as Linked below.
    if leads:
        named = ", ".join(
            _mention(lead.display_name, lead.telegram_user_id) for lead in leads
        )
        lines.append(f"<b>Contact</b>  {named}")

    # Only when there is something to say. Most tickets are linked to nothing,
    # and a permanent "Linked: none" would be a line of noise on every header
    # to save a moment's thought on a few.
    if linked_references:
        lines.append(f"<b>Linked</b>  {e(', '.join(linked_references))}")
    return "\n".join(lines)


async def _leads_for_item(session: AsyncSession, item: WorkItem) -> list:
    """The named contacts in the group this request came from.

    Imported here rather than at module scope because `app.bot.registry`
    imports from the service layer, and the other direction at import time is
    a cycle. The registry is the right home for it - it is a question about
    who people are, not about relaying.
    """
    from app.bot.registry import leads_for

    source, _ = await chats_for(session, item)
    return await leads_for(session, source)


async def refresh_header(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem
) -> None:
    """Rewrite the topic header, and the traffic light, to match the item.

    Both together, in one place, because they answer the same question from
    two distances - the light for someone scanning the list, the header for
    someone who has opened it. Kept apart they would drift, and a title saying
    amber above a header saying Closed is worse than neither.
    """
    _, ops = await chats_for(session, item)
    client = await session.get(Client, item.client_id)

    # Before the header, and before the early return below: a request with no
    # header message still has a title, and the light still has to be right.
    if item.topic_id is not None:
        try:
            await gateway.rename_topic(
                ops.telegram_chat_id,
                item.topic_id,
                topic_name(item, client.name if client else "Unknown client"),
            )
        except Exception:
            logger.debug(
                "Could not retitle topic for %s", item.display_reference, exc_info=True
            )

    if item.header_message_id is None:
        return
    owner = await session.get(Staff, item.owner_staff_id) if item.owner_staff_id else None

    linked = [other.display_reference for other in await wi.linked_to(session, item)]

    try:
        await gateway.edit_message_text(
            ops.telegram_chat_id,
            item.header_message_id,
            header_text(
                item,
                client.name if client else "Unknown client",
                owner.display_name if owner else None,
                linked_references=linked,
                owner_telegram_user_id=owner.telegram_user_id if owner else None,
                leads=await _leads_for_item(session, item),
            ),
            parse_mode="HTML",
        )
    except Exception:
        # An unchanged message, or one too old to edit. The announcements in
        # the topic still carry the change, so this is cosmetic.
        logger.debug("Could not refresh header for %s", item.display_reference, exc_info=True)


def closure_text(item: WorkItem, resolution: str | None = None) -> str:
    """What the client is told when a request is closed.

    NexterPay asked for the original request to be repeated back, because a
    bare "this is now closed" arriving days later means nothing to whoever
    reads it. The line on what was done is optional: the person closing adds
    one if there is something worth saying, and skips it if there is not.
    """
    raised = item.created_at.strftime("%d %B") if item.created_at else "earlier"
    original = " ".join((item.original_message or "").split())
    if len(original) > 400:
        original = original[:399].rstrip() + "…"

    parts = [
        f"Request {item.client_reference} is now resolved.",
        "",
        f"What you raised on {raised}:",
        f'"{original}"',
    ]
    if resolution:
        parts += ["", "What we did:", resolution.strip()]
    parts += ["", "If anything is still outstanding, reply to this message."]
    return "\n".join(parts)


def acknowledgement_text(item: WorkItem) -> str:
    """What a counterparty sees when their request is opened.

    Business closes differently, at NexterPay's request. A commercial enquiry
    is a conversation being started rather than a fault being reported, and
    "add anything further to it" is the wrong invitation when what the person
    wants to know is that someone is coming back to them.
    """
    closing = (
        "One of the Business team will get back to you. Reply to this message "
        "to add anything further."
        if item.department is Department.BUSINESS
        else "Please reply to this message to add anything further to it."
    )
    return (
        f"Request {item.client_reference} has been logged with our "
        f"{item.department.label} team.\n\n{closing}"
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
    ack_keyboard=None,
    context: str | None = None,
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
        # No buttons here, ever, and no parameter to add them with.
        #
        # The header is rewritten on every claim, status change, priority
        # change and link, by `refresh_header` calling `edit_message_text` -
        # which Telegram reads as "this message has no keyboard now". Buttons
        # put here survive until the first thing that happens to the request.
        #
        # They go on a separate "Actions:" message that nothing edits. This
        # parameter existed and was always None; it is gone so that the next
        # person cannot find it and use it, which is how open_internal broke.
        parse_mode="HTML",
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

    if context:
        # Posted before the client's words, because without it their words may
        # make no sense on their own. "why so?" is a real example.
        await gateway.send_message(
            ops.telegram_chat_id, context, thread_id=thread_id
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
        source_chat.telegram_chat_id,
        acknowledgement_text(item),
        reply_markup=ack_keyboard,
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
    replying_to: str | None = None,
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
    #
    # On a closed request the person to reach is whoever closed it rather than
    # whoever owned it, since they made the judgement that it was finished.
    if item.status is WorkItemStatus.CLOSED:
        owner = await _closed_by(session, item) or (
            await session.get(Staff, item.owner_staff_id)
            if item.owner_staff_id is not None
            else None
        )
    else:
        owner = (
            await session.get(Staff, item.owner_staff_id)
            if item.owner_staff_id is not None
            else None
        )

    if text:
        # The counterparty's own words go in a blockquote.
        #
        # NexterPay asked for this message "in a different colour". Telegram
        # gives a bot no colour at all - the entire set of styles available is
        # bold, italic, underline, strikethrough, spoiler, code, pre,
        # blockquote, links and mentions, and not one of them changes the
        # colour of text. A blockquote is the strongest thing on that list:
        # Telegram draws it as an indented block with a vertical bar down the
        # side, which is what actually separates it from the run of
        # bot chatter around it.
        #
        # It is also the honest markup. This is somebody else's words quoted
        # into our group, which is exactly what a blockquote means, so it will
        # keep making sense to a reader who never heard the request behind it.
        quoted = f"<blockquote>{html.escape(text)}</blockquote>"

        # What they were replying to, when it was not simply the last thing we
        # said.
        #
        # NexterPay asked for this on 5 September. Telegram shows the client
        # the message they are quoting; we were passing on only what they
        # typed. So a client answering one specific reply among several -
        # "no, the other one" - arrived as "no, the other one" and nothing
        # else, and whoever picked it up had to guess.
        #
        # Trimmed hard: it is context, not content, and the message it
        # belongs to is a few lines up the topic anyway.
        context = ""
        if replying_to:
            flattened = " ".join(replying_to.split())
            if len(flattened) > 160:
                flattened = flattened[:159].rstrip() + "…"
            context = f"\n<i>in reply to: {html.escape(flattened)}</i>"

        who = f"<b>{html.escape(sender_name)} has replied</b> on {item.display_reference}"
        if owner is not None:
            body = f"{mention_for(owner)} — {who}{context}\n{quoted}"
        else:
            body = f"{who}{context}\n{quoted}"
        await gateway.send_message(
            ops.telegram_chat_id, body, thread_id=item.topic_id, parse_mode="HTML"
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

    if item.status is WorkItemStatus.CLOSED:
        # NexterPay's decision: a reply to a closed request does not reopen it.
        # The person who closed it is notified and decides. The client is told
        # rather than left wondering - we invited the reply, so silence here
        # would be worse than not inviting it at all.
        note = (
            f"{item.client_reference} is already closed, so this has been passed to "
            f"the person who handled it rather than reopening the request. "
            f"If it needs to be looked at again, they will come back to you."
        )
        sent = await gateway.send_message(source.telegram_chat_id, note)
        await _record_message(
            session, item,
            direction=MessageDirection.OUTBOUND,
            chat_id=source.telegram_chat_id,
            message_id=sent.message_id,
            sender_name="NexterPay Operations",
            text=note,
        )


async def send_client_reply(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    actor: Actor,
    text: str,
    *,
    attachment: IncomingAttachment | None = None,
    tag_lead: int | None = None,
) -> None:
    """The only path from NexterPay to a client.

    The reply carries the reference and becomes the new anchor, so replying to
    it resolves back to this work item.

    `tag_lead` is the Telegram id of one named contact, addressed by name so
    they are notified rather than relying on somebody noticing. Off by
    default: whether a particular message needs one person's attention is a
    decision per message, and tagging the same person on every reply teaches
    them to ignore it.

    One person, not all of them. It used to take a boolean and mention every
    named contact, while the button offering it was labelled with the first -
    so "Send and tag Ann" mentioned Ann, Ben and Cara. A button that does more
    than its label says is worst on this screen of all, which exists to stop
    people tapping without reading.
    """
    actor.require_any()
    source, ops = await chats_for(session, item)
    # client_reference, not display_reference: an outbound message must never
    # carry the supplier code. See the note on the property.
    outbound = f"{item.client_reference} — {text}"

    parse_mode = None
    if tag_lead is not None:
        from app.bot.registry import leads_for

        leads = [
            lead for lead in await leads_for(session, source)
            if lead.telegram_user_id == tag_lead
        ]
        if leads:
            # Everything interpolated is escaped: the reference is ours, but
            # `text` is whatever a member of staff typed, and a stray "<" would
            # otherwise be swallowed as markup or rejected by Telegram.
            named = ", ".join(
                f'<a href="tg://user?id={lead.telegram_user_id}">'
                f"{html.escape(lead.display_name)}</a>"
                for lead in leads
            )
            outbound = (
                f"{html.escape(item.client_reference)} — {named} — {html.escape(text)}"
            )
            parse_mode = "HTML"

    sent = await gateway.send_message(
        source.telegram_chat_id, outbound, parse_mode=parse_mode
    )
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


async def reopen(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, actor: Actor
) -> None:
    """Put a closed request back into play. Manager and above."""
    if item.status is not WorkItemStatus.CLOSED:
        return

    _, ops = await chats_for(session, item)
    before = await _last_event_id(session, item)
    await wi.reopen(session, item, actor)

    if item.topic_id is not None:
        await gateway.reopen_topic(ops.telegram_chat_id, item.topic_id)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)


async def _closed_by(session: AsyncSession, item: WorkItem):
    """Whoever closed it, so a client chasing afterwards reaches that person."""
    from sqlalchemy import select

    result = await session.execute(
        select(Event)
        .where(Event.work_item_id == item.id, Event.event_type == EventType.WORK_ITEM_CLOSED)
        .order_by(Event.id.desc())
        .limit(1)
    )
    event = result.scalar_one_or_none()
    if event is None or event.actor_staff_id is None:
        return None
    return await session.get(Staff, event.actor_staff_id)


def outbound_opening_text(item: WorkItem, body: str) -> str:
    """What the counterparty receives when NexterPay raise something with them.

    Deliberately not the acknowledgement wording. "Request X has been logged
    with our Support team" is nonsense when we are the ones raising it.
    """
    return (
        f"{item.client_reference} · {item.subject}\n\n"
        f"{body}\n\n"
        f"Reply to this message to respond."
    )


async def open_internal(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    origin: WorkItem,
    department: Department,
    subject: str,
    body: str,
    actor: Actor,
    keyboard_for=None,
) -> WorkItem:
    """Ask another department to look at something, on the same client.

    NexterPay's answer to moving a request between desks, and a better one
    than the question. Dragging a live request across means carrying its
    topic, its history and the client's view of it into another Operations
    Group and hoping all three arrive. Opening a fresh request instead reuses
    everything that already works, and linking the two means the client still
    sees one thread while two desks work on it.

    Nothing here reaches the counterparty. The client raised one thing; that
    NexterPay asked Finance about it is an internal fact, and the new request
    lives entirely inside the Operations Group of the department being asked.
    `test_only_these_functions_may_write_to_a_client_chat` holds the list of
    functions permitted to write outward, and this is deliberately not on it.
    """
    actor.require_any()

    source, _ = await chats_for(session, origin)
    item = await wi.create_work_item(
        session,
        source_chat=source,
        subject=subject,
        original_message=body,
        raised_by_name=actor.name,
        raised_by_telegram_user_id=actor.telegram_user_id,
        department=department,
    )
    item.raised_by_us = True
    item.supplier_id = origin.supplier_id
    item.supplier_code = origin.supplier_code
    item.asked_from_id = origin.id
    await session.flush()

    client = await session.get(Client, item.client_id)
    client_name = client.name if client else "Unknown client"
    _, ops = await chats_for(session, item)

    thread_id = await gateway.create_topic(
        ops.telegram_chat_id, topic_name(item, client_name)
    )
    await wi.attach_topic(session, item, thread_id)

    header = await gateway.send_message(
        ops.telegram_chat_id,
        header_text(item, client_name),
        thread_id=thread_id,
        parse_mode="HTML",
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

    # The question, and underneath it the thing the client actually asked.
    #
    # NexterPay's point on 5 September: forwarding to a department showed only
    # the note typed by whoever asked, not the original request. Finance were
    # being asked to confirm a rate with no sight of why anybody wanted it -
    # so the first thing they did was go and find the other ticket, which is
    # the work this feature exists to save.
    #
    # The origin's own words, quoted, and attributed to whoever raised it.
    # Trimmed, because a client who wrote four paragraphs should not push the
    # question off the screen; the full text is one tap away in the linked
    # request.
    original = " ".join((origin.original_message or "").split())
    if len(original) > 600:
        original = original[:599].rstrip() + "…"

    context_lines = [
        f"↳ {html.escape(actor.name)} asked {department.label} about "
        f"{html.escape(origin.display_reference)}:",
        html.escape(body),
    ]
    if original:
        raiser = origin.raised_by_name or "the client"
        who = "we raised it" if origin.raised_by_us else f"{html.escape(raiser)} raised it"
        context_lines += [
            "",
            f"<b>{html.escape(origin.display_reference)}</b>, as {who}:",
            f"<blockquote>{html.escape(original)}</blockquote>",
        ]

    await gateway.send_message(
        ops.telegram_chat_id,
        "\n".join(context_lines),
        thread_id=thread_id,
        parse_mode="HTML",
    )

    # Linked immediately rather than left to somebody to remember. The whole
    # point of raising rather than transferring is that both desks can see the
    # other half, and a link nobody makes is not a link.
    await link(session, gateway, origin, item, actor)

    # The buttons go on their own message, last, exactly as `open_request`
    # does it. Two goes at this were wrong before it landed here.
    #
    # First they were passed in ready-made, built before the row existed, so
    # every one of them encoded work item 0 and did nothing.
    #
    # Then they were attached to the header - and stripped again three lines
    # later by `link`, which calls `refresh_header`, which calls
    # `edit_message_text` without a reply_markup. Telegram treats that as
    # "this message now has no keyboard". The header is a live document that
    # gets rewritten whenever ownership, status, priority or links change;
    # anything durable put on it is on borrowed time.
    #
    # A separate message is not touched by any of that, and it is why normal
    # requests never had the problem.
    if keyboard_for is not None:
        await gateway.send_message(
            ops.telegram_chat_id,
            "Actions:",
            thread_id=thread_id,
            reply_markup=keyboard_for(item.id),
        )
    return item


async def answer_internal(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    actor: Actor,
    text: str,
) -> WorkItem | None:
    """Send an answer back to the desk that asked.

    The other half of `open_internal`, and it was missing. Asking another
    department opened a linked request on their desk and stopped there: the
    answer sat in their topic, and whoever asked had to know to go and read
    it. NexterPay chose asking over transferring specifically because the
    answer comes back, so a version that does not is not the feature they
    agreed to.

    Goes to the Operations Group of the asking desk, into the topic of the
    request that was asked about. Never to the counterparty - the client asked
    Support a question, and that Finance was consulted is an internal fact.
    The desk holding the client relationship decides what, if any, of this the
    client is told, which is why `send_client_reply` is still the only route
    outward and it belongs to them.

    Returns the origin so the caller can name it, or None if there is nothing
    to answer - which is not an error. Somebody may reasonably tap Answer on a
    request that was raised directly rather than asked for.
    """
    actor.require_any()
    if item.asked_from_id is None:
        return None

    origin = await session.get(WorkItem, item.asked_from_id)
    if origin is None or origin.topic_id is None:
        logger.warning(
            "%s was asked from %s, which has no topic to answer into",
            item.display_reference, item.asked_from_id,
        )
        return None

    _, origin_ops = await chats_for(session, origin)

    owner = (
        await session.get(Staff, origin.owner_staff_id)
        if origin.owner_staff_id is not None
        else None
    )
    # Mention the person waiting on it, exactly as a client reply does. An
    # answer nobody is told about is the same problem one step further along.
    lead = f"{mention_for(owner)} — " if owner is not None else ""

    await gateway.send_message(
        origin_ops.telegram_chat_id,
        f"{lead}<b>{html.escape(item.department.label)} answered</b> on "
        f"{html.escape(item.display_reference)}\n"
        f"<blockquote>{html.escape(text)}</blockquote>",
        thread_id=origin.topic_id,
        parse_mode="HTML",
    )

    # Recorded on both. On the answering request because it is what that
    # request was for, and on the origin because somebody reading its history
    # a month later should not have to open another ticket to find the answer.
    await wi.record_event(
        session, item, EventType.INTERNAL_ANSWER_SENT, actor,
        text=text[:500], to_reference=origin.display_reference,
    )
    await wi.record_event(
        session, origin, EventType.INTERNAL_ANSWER_SENT, actor,
        text=text[:500], from_reference=item.display_reference,
    )
    return origin


async def open_outbound(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    counterparty_chat: Chat,
    subject: str,
    body: str,
    actor: Actor,
    tag_lead: int | None = None,
) -> WorkItem:
    """A request NexterPay raise with a client or supplier.

    The mirror of open_request. Same work item, same topic, same everything
    afterwards - the only differences are who wrote the first message and
    which way the arrow points at the start.

    The message posted into their group is recorded, so their reply resolves
    through the routing that already exists rather than needing its own.
    """
    actor.require_any()

    item = await wi.create_work_item(
        session,
        source_chat=counterparty_chat,
        subject=subject,
        original_message=body,
        raised_by_name=actor.name,
        raised_by_telegram_user_id=actor.telegram_user_id,
    )
    item.raised_by_us = True
    await session.flush()

    client = await session.get(Client, item.client_id)
    client_name = client.name if client else "Unknown counterparty"
    _, ops = await chats_for(session, item)

    thread_id = await gateway.create_topic(ops.telegram_chat_id, topic_name(item, client_name))
    await wi.attach_topic(session, item, thread_id)

    header = await gateway.send_message(
        ops.telegram_chat_id,
        header_text(item, client_name),
        thread_id=thread_id,
        # No buttons here, ever, and no parameter to add them with.
        #
        # The header is rewritten on every claim, status change, priority
        # change and link, by `refresh_header` calling `edit_message_text` -
        # which Telegram reads as "this message has no keyboard now". Buttons
        # put here survive until the first thing that happens to the request.
        #
        # They go on a separate "Actions:" message that nothing edits. This
        # parameter existed and was always None; it is gone so that the next
        # person cannot find it and use it, which is how open_internal broke.
        parse_mode="HTML",
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
        f"↳ Raised by {actor.name} with {client_name}:\n{body}",
        thread_id=thread_id,
    )

    outbound = outbound_opening_text(item, body)

    # Addressed to the named contact, if asked for. Same shape as a reply,
    # and NexterPay's reasoning is that an opening message is the one most
    # likely to need a person rather than a room - somebody has to decide to
    # act on it, and nobody has been watching for it.
    #
    # Still a choice per message rather than automatic. Tagging the same
    # person on everything teaches them to ignore it, which costs more than
    # it buys.
    parse_mode = None
    if tag_lead is not None:
        from app.bot.registry import leads_for

        leads = [
            lead for lead in await leads_for(session, counterparty_chat)
            if lead.telegram_user_id == tag_lead
        ]
        if leads:
            named = ", ".join(
                f'<a href="tg://user?id={lead.telegram_user_id}">'
                f"{html.escape(lead.display_name)}</a>"
                for lead in leads
            )
            outbound = f"{named} —\n{html.escape(outbound)}"
            parse_mode = "HTML"

    sent = await gateway.send_message(
        counterparty_chat.telegram_chat_id, outbound, parse_mode=parse_mode
    )
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=counterparty_chat.telegram_chat_id,
        message_id=sent.message_id,
        sender_name=actor.name,
        text=outbound,
    )
    return item


async def open_requests_for(
    session: AsyncSession, source_chat: Chat, *, recent_closed: bool = False
) -> list[WorkItem]:
    """Requests raised from this client group, oldest first.

    The whole group rather than the person asking: they can already read each
    other's messages in there, so hiding a colleague's request would be
    theatre rather than privacy.

    `recent_closed` adds anything resolved in the last four weeks. NexterPay
    chose that window: long enough to answer "what happened to the thing from
    a fortnight ago", short enough that a group running for a year does not
    reply with a wall of text nobody reads.
    """
    from sqlalchemy import select

    live = WorkItem.status.not_in([WorkItemStatus.CLOSED, WorkItemStatus.COMPLETED])
    if recent_closed:
        since = utcnow() - wi.CLIENT_HISTORY
        condition = live | (
            WorkItem.closed_at.is_not(None) & (WorkItem.closed_at >= since)
        )
    else:
        condition = live

    result = await session.execute(
        select(WorkItem)
        .where(WorkItem.source_chat_id == source_chat.id, condition)
        .order_by(WorkItem.reference)
    )
    return list(result.scalars().all())


async def post_anchor(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem
) -> None:
    """Post a fresh message in the client group that replies will attach to.

    A client with several requests open should not have to scroll back to find
    our original acknowledgement. Tapping a request from the list posts a new
    anchor at the bottom of the conversation, and because it is recorded
    against the work item the existing reply routing resolves it - no new
    mechanism, and nothing to go wrong differently from the path that already
    works.
    """
    source, _ = await chats_for(session, item)
    text = (
        f"{item.client_reference} · {item.subject}\n"
        f"Status: {item.status.client_label}\n\n"
        f"Reply to this message to add to this request."
    )
    sent = await gateway.send_message(source.telegram_chat_id, text)
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=source.telegram_chat_id,
        message_id=sent.message_id,
        sender_name="NexterPay Operations",
        text=text,
    )


async def file_under(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    supplier: Client,
    actor: Actor,
) -> None:
    """Record which supplier a ticket concerns, and rename it accordingly.

    NexterPay file requests as Client / Supplier / Ticket. The supplier is set
    after the fact, by staff, because the client raising a request does not
    know which supplier it concerns and frequently nobody does until someone
    has looked at it.

    Filing changes the reference, so the topic title is rewritten to match.
    Otherwise the ticket would answer to one name in conversation and another
    in the sidebar, which defeats the point of filing it at all.
    """
    if supplier.code is None:
        raise DomainError(
            f"{supplier.name} has no code yet. An administrator can set one with "
            f"/np_setcode inside their group."
        )
    if item.supplier_id == supplier.id:
        return

    was = item.display_reference
    item.supplier_id = supplier.id
    item.supplier_code = supplier.code
    await session.flush()

    await wi.record_event(
        session, item, EventType.SUPPLIER_FILED, actor,
        supplier=supplier.name,
        supplier_code=supplier.code,
        from_reference=was,
        to_reference=item.display_reference,
    )

    # The retitle used to happen here as well. It now lives in refresh_header,
    # which runs a line below and rebuilds the title from the item - so the new
    # reference and the traffic light are applied in one call rather than two
    # that could disagree.
    await announce(session, gateway, item, await _latest_event(session, item))
    await refresh_header(session, gateway, item)


async def link(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    other: WorkItem,
    actor: Actor,
) -> None:
    """Tie two tickets together, visibly from both sides.

    Everything here is internal. A link is a note to NexterPay's own team that
    two pieces of work are the same problem; the client whose ticket it is has
    no business knowing that their issue is filed alongside another client's,
    and the other ticket's reference can carry a supplier code. So nothing is
    written to a counterparty group by this function, and `test_linking`
    asserts that rather than trusting the comment.

    Both topics are updated, because a link visible from one side only would
    not be the thing that was agreed. If the two tickets belong to different
    departments, that means writing into two different Operations Groups -
    both internal, both NexterPay's own.
    """
    await wi.link_tickets(session, item, other, actor)

    for this in (item, other):
        await announce(session, gateway, this, await _latest_event(session, this))
        await refresh_header(session, gateway, this)


async def unlink(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    other: WorkItem,
    actor: Actor,
) -> bool:
    """Remove a link, from both sides. False if there was not one.

    Any member of staff can undo one. The events stay either way, so a link
    made in error can be taken off the header without taking it out of the
    record - which is what makes it safe to let people correct themselves
    rather than escalating a typo to a manager.
    """
    removed = await wi.unlink_tickets(session, item, other, actor)
    if not removed:
        return False

    for this in (item, other):
        await announce(session, gateway, this, await _latest_event(session, this))
        await refresh_header(session, gateway, this)
    return True


async def close(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, actor: Actor,
    *, notify_client: bool | None = None, resolution: str | None = None,
) -> None:
    """Close the work item, tell the client, and archive the topic.

    NexterPay decided that clients are told, with the original request
    repeated back and an optional line on what was done. Business is the
    exception and closes silently: there the answer itself is the conclusion,
    and a closure notice would be noise.
    """
    if notify_client is None:
        notify_client = item.department is not Department.BUSINESS
    if item.status is WorkItemStatus.CLOSED:
        # Second tap on a Close button that is still on screen. wi.close() is
        # already idempotent, but everything after it was not: the client was
        # sent a second "your request has been closed" message before Telegram
        # rejected the duplicate topic close. Telling a customer twice that
        # their case is shut is worse than the crash that revealed it.
        logger.info("%s is already closed; ignoring", item.display_reference)
        return

    source, ops = await chats_for(session, item)
    before = await _last_event_id(session, item)
    await wi.close(session, item, actor)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)

    if notify_client:
        sent = await gateway.send_message(
            source.telegram_chat_id, closure_text(item, resolution)
        )
        await _record_message(
            session, item,
            direction=MessageDirection.OUTBOUND,
            chat_id=source.telegram_chat_id,
            message_id=sent.message_id,
            sender_name="NexterPay Operations",
            text=closure_text(item, resolution),
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
