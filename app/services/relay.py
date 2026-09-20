"""The relay: everything that moves between a client group and a topic.

Safety rule, and the reason this module is small and explicit: **the only
route by which anything a member of staff wrote reaches a client is
`send_client_reply`.** Internal notes and staff discussion have no path
outward, by construction rather than by convention. `tests/test_relay.py`
asserts this directly.

A short, named set of functions also writes to a client chat, but only ever
with text this module composes itself: the acknowledgement in `open_request`,
the opening message in `open_outbound`, the anchor in `post_anchor`, the
closure notice in `close`, the note in `relay_client_message` telling someone
a request is already closed, and the line in `claim` naming who has picked it
up. Nothing in that list can carry staff wording - `claim` interpolates a
staff member's display name, which NexterPay set on the record, and nothing
else. `test_only_these_functions_may_write_to_a_client_chat` holds the same
list and fails if a seventh appears.

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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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


async def counterparty_chats(session: AsyncSession, item: WorkItem) -> list[Chat]:
    """Every outside group this request reaches. One, or two if it is bridged.

    Filing Structure and Connected Tickets, section 4. A two-sided request has
    the client's group and the supplier's, with one topic in Operations where
    both halves are visible.

    This list is what the platform's outward safety now rests on. It used to
    rest on there being only one group and no way to address another; the list
    is the replacement, and `send_client_reply` refuses anything not in it.

    The raising group is always first, so a caller that does not care which
    side it is talking to gets the behaviour every one-sided request has always
    had.
    """
    source, _ = await chats_for(session, item)
    chats = [source]
    if item.bridged_chat_id is not None and item.bridged_chat_id != item.source_chat_id:
        other = await session.get(Chat, item.bridged_chat_id)
        if other is not None:
            chats.append(other)
    return chats


async def reference_for(
    session: AsyncSession, item: WorkItem, chat: Chat
) -> str:
    """The reference a particular counterparty is shown.

    Each side sees a reference built from **their own** four-letter code, never
    the other side's.

    Found live on 16 September, on the first two-sided ticket ever sent. The
    words did not cross - the whole design saw to that - but the reference did:
    a reply to the supplier went out as "ACME-1072", handing them the client's
    code. A supplier who knows the work is for ACME knows whose business it is,
    which is the thing the Filing Structure note says must stay internal, and
    the reason the FX build has a `supplier_reference` of its own.

    The tests did not catch it. They checked that the message body did not
    reach the wrong group and never looked at what was wrapped around it.
    """
    if chat.id == item.source_chat_id:
        return item.client_reference

    await session.refresh(chat, ["client"])
    code = chat.client.code if chat.client else None
    return f"{code}-{item.reference}" if code else f"#{item.reference}"


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
    origin_message_id: int | None = None,
) -> Message:
    message = Message(
        work_item_id=item.id,
        direction=direction,
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        sender_name=sender_name,
        sender_telegram_user_id=sender_telegram_user_id,
        text=text,
        origin_message_id=origin_message_id,
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


# The stage symbol, beside the light.
#
# NexterPay, 14 September, having mocked up coloured bubbles with symbols in
# them. Telegram gives a topic either a coloured bubble or a custom emoji icon
# and not both, and the colour is fixed at creation and cannot be edited - so
# the bubble could carry the symbol only by giving up the blue. They asked for
# both, which means the symbol goes on the name, where the light already is.
#
# The two say different things and that is why both are here. The light answers
# "is anybody on this", which is the triage question. Amber then covers
# claimed, in progress, waiting on three different parties and escalated
# alike - so the symbol answers "how far has it got", which amber cannot.
#
# All four exist in Telegram's own topic-icon set, checked against
# getForumTopicIconStickers on 14 September. That is deliberate: if NexterPay
# ever prefer the symbol as the bubble after all, the vocabulary already
# transfers and only the placement changes.
SYMBOL_UNCLAIMED = "📝"
SYMBOL_WORKING = "👀"
SYMBOL_RESOLVED = "✅"
SYMBOL_CLOSED = "🏁"


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


def status_symbol(item: WorkItem) -> str:
    """How far along it is, which the light deliberately does not say.

    Resolved and closed are kept apart here even though the light calls them
    amber and green. NexterPay were asked directly whether work-finished-but-
    not-archived counts as green and said no - but "we have fixed it" and "this
    is over" are still different things to a person scanning the list, and the
    symbol is where that difference now lives.
    """
    if item.status is WorkItemStatus.CLOSED:
        return SYMBOL_CLOSED
    if item.status is WorkItemStatus.COMPLETED:
        return SYMBOL_RESOLVED
    if item.status is WorkItemStatus.OPEN and item.owner_staff_id is None:
        return SYMBOL_UNCLAIMED
    return SYMBOL_WORKING


def topic_name(item: WorkItem, client_name: str) -> str:
    """What the topic is called in the list, which is where triage happens.

    The status symbol and the priority mark both go here, and for the same
    reason. NexterPay asked for High to stand out; it was built into the header
    only, where you have to open a request to see it. A priority you cannot see
    while scanning is a priority you cannot sort by, which leaves it doing
    nothing that the header's own status line was not already doing.

    Mark after the symbol, not before: the symbol answers "is anyone on this",
    which is the first question, and every topic has one. Only two priorities
    in five carry a mark, so leading with it would ragged the list.

    This said "light" until 18 September, describing the traffic light that was
    removed on the 15th - the comments below the signature had been updated and
    the docstring had not. Left as a note because the docstring is what gets
    read first and was the last thing still claiming the dots were there.
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
    # Symbol, then the priority mark, then the reference.
    #
    # The traffic light used to lead this. NexterPay asked for it on
    # 5 September and asked for it out again on 15 September - "the bubbles
    # still have the colour inside them, remove the dots" - because the topic
    # already carries a coloured bubble and a second coloured dot beside it
    # reads as noise.
    #
    # Worth knowing what went with it: a topic's bubble colour is fixed when
    # the topic is created and cannot be edited afterwards, so nothing in the
    # list changes colour as a request progresses any more. The symbol is now
    # the only moving part, which is why it carries four states rather than
    # three.
    #
    # `traffic_light` is kept - it is still a correct description of a request
    # and is cheap to hold - but nothing renders it today.
    return (
        f"{status_symbol(item)}{mark or ''} "
        f"{item.display_reference} · {item.subject}"
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


def closure_text(
    item: WorkItem,
    resolution: str | None = None,
    reference: str | None = None,
    *,
    raised_it: bool = True,
) -> str:
    """What a counterparty is told when a request is closed.

    `reference` names the one this particular side is shown. On a two-sided
    request the other side is told too, and must be told using their own code -
    the same rule that applies to every other message leaving the platform.

    NexterPay asked for the original request to be repeated back, because a
    bare "this is now closed" arriving days later means nothing to whoever
    reads it. The line on what was done is optional: the person closing adds
    one if there is something worth saying, and skips it if there is not.

    **`raised_it` is a leak guard, not a wording preference.** The original
    message belongs to whoever raised the request. On a two-sided request the
    other side did not raise it and has never seen it, so repeating it to them
    would forward a client's words to a supplier with nobody deciding to - the
    exact thing section 4's first rule forbids.

    Found on 17 September by closing a bridged ticket and reading what the
    supplier got: "What you raised on 16 September: 'bridge reference check'".
    Wrong twice over - they had raised nothing, and those were the client's
    words.
    """
    shown = _e(reference or item.client_reference)
    parts = [f"{MARK_RESOLVED} <b>Request {shown} is now resolved.</b>"]

    if raised_it:
        raised = item.created_at.strftime("%d %B") if item.created_at else "earlier"
        original = " ".join((item.original_message or "").split())
        if len(original) > 400:
            original = original[:399].rstrip() + "…"
        # Their own words, in italics and quoted, as NexterPay drew it. Escaped
        # because this is the single most likely place for a stray "<" on the
        # whole platform: it is whatever the client typed, played back to them.
        parts += ["", f"What you raised on {_e(raised)}:", f"<i>“{_e(original)}”</i>"]
    else:
        # No quotation, and no summary of one either. "The matter you were
        # helping with" is as far as this can go without describing something
        # they were never told.
        parts += ["", "Thank you for your help with it."]

    if resolution:
        parts += ["", "What we did:", _e(resolution.strip())]
    parts += ["", OUTSTANDING_HINT]
    return "\n".join(parts)


# The markers on the front of every message that leaves the platform.
#
# NexterPay's design, drawn as a before-and-after on 19 September. The point is
# not decoration: a counterparty group carries ordinary conversation as well as
# ours, and a request that opens with a symbol is findable by scrolling. They
# are constants because they are wording, and wording changes - this is the one
# place to change it.
MARK_RECEIVED = "📥"
MARK_RESPONSE = "💬"
MARK_RESOLVED = "✅"
MARK_OWNER = "👤"

# The invitation, in brackets and on its own line.
#
# It was a plain sentence run on from the message before it, which read as part
# of the answer rather than as an instruction about the group. NexterPay's
# mockup sets it apart, and they are right: it is the only line in any of these
# messages that tells somebody what to *do*.
#
# Italic for the aside, bold for the instruction inside it, at NexterPay's
# request on 20 September. The nesting is the point: the brackets say "this is
# not part of the answer", the bold says "this is the bit that matters".
REPLY_HINT = (
    "<i>(Please <b>reply to this message</b> if you would like to add "
    "anything further.)</i>"
)
OUTSTANDING_HINT = (
    "<i>(If anything is still outstanding, <b>reply to this message</b>.)</i>"
)

# Everything below composes HTML, which changes the rules for all of it.
#
# `parse_mode` is off by default on the gateway for a good reason, written
# there: most of what this bot sends is text a client or a member of staff
# typed, and a stray "<" is either swallowed as markup or rejected outright by
# Telegram - which means a message that silently does not arrive.
#
# So from here on every interpolated value is escaped, without exception, and
# the only unescaped things are the tags themselves. The values that actually
# matter are `original_message` and `resolution` in a closure, and `text` in a
# reply: those are somebody's typing, and "amount < 500 & rising" is a
# perfectly ordinary thing for a client to write.
def _e(value: object) -> str:
    """Escape anything going into an HTML message. Never optional.

    `quote=False` on purpose. Telegram asks for exactly three replacements -
    "<" with &lt;, ">" with &gt; and "&" with &amp; - and escaping quotes on
    top of that is noise in messages that are full of quoted client text. A
    closure plays back what somebody wrote, in quotation marks, so this is the
    difference between reading their words and reading &quot;their words&quot;
    if a client ever sees an entity Telegram does not decode.
    """
    return html.escape(str(value), quote=False)


def acknowledgement_text(item: WorkItem) -> str:
    """What a counterparty sees when their request is opened.

    "Has been received... will review your request" rather than "has been
    logged with our team". NexterPay's wording, and the better of the two: a
    person who has just reported a problem wants to know somebody will look at
    it, not that a record exists.

    Business reads differently, at NexterPay's request. A commercial enquiry is
    a conversation being started rather than a fault being reported, and "add
    anything further to it" is the wrong invitation when what the person wants
    to know is that someone is coming back to them.
    """
    # The title is bold and the sentence after it is not, at NexterPay's
    # request on 20 September. The title is the part somebody scanning a busy
    # group needs to find; the rest is courtesy.
    if item.department is Department.BUSINESS:
        title = f"Enquiry {_e(item.client_reference)} has been received."
        rest = f"One of our {_e(item.department.label)} Team will get back to you."
    else:
        title = f"Request {_e(item.client_reference)} has been received."
        rest = f"Our {_e(item.department.label)} Team will review your request."
    return f"{MARK_RECEIVED} <b>{title}</b> {rest}\n\n{REPLY_HINT}"


def staff_reply_text(
    reference: str,
    text: str,
    *,
    sender: str | None = None,
    mention: str | None = None,
    escape: bool = False,
) -> str:
    """A reply from the desk, as the counterparty reads it.

    Was `ACME-1042 — from Sarah Hill — <text>`, one run-on line. NexterPay
    redrew it on 19 September as a header, the message, then the invitation,
    and it reads far better: the answer is the thing somebody wants, and it now
    starts on its own line instead of after two pieces of routing.

    **The name stays, in the header.** The mockup showed `Response to
    ACME-1098` with no name, and dropping it was the first version of this.
    Two tests failed, and they were right to: NexterPay asked for signed
    replies on 5 September — "the client should know who they are speaking
    with, more personal" — and asked again for Business specifically, where a
    negotiation is the most personal conversation on the platform. A drawn
    example of one message is not the place to read a reversal of that into.
    So the redraw is the shape, and the name moves into the header rather than
    out of the message.

    It is the staff member's display name from their record, not their Telegram
    name, so NexterPay control what a client sees.

    Everything interpolated is escaped, and that is no longer optional. It used
    to be, because only the tagged-contact path composed HTML; from
    20 September every message out is HTML, so `text` - whatever a member of
    staff typed - is escaped on every path. "amount < 500 & rising" is an
    ordinary thing to write and would otherwise be swallowed as markup or
    rejected outright by Telegram, which means a reply that silently never
    arrives. `mention` is already markup and is the one thing not escaped.

    `escape` is kept as a parameter and ignored, so that any caller still
    passing it keeps working; it will go once nothing does.
    """
    signature = f" — from {_e(sender)}" if sender else ""
    body = f"{mention} — {_e(text)}" if mention else _e(text)
    return (
        f"{MARK_RESPONSE} <b>Response to {_e(reference)}{signature}</b>\n\n"
        f"{body}\n\n"
        f"{REPLY_HINT}"
    )


def claim_notice_text(item: WorkItem, actor_name: str | None) -> str | None:
    """What a counterparty is told when somebody picks their request up.

    A function rather than three lines inside `claim` so that it can be read
    without a database. The documents that go to NexterPay are checked against
    it, and a check that quotes a message it cannot call is a check that
    passes while the wording drifts - which is exactly what happened here on
    7 September before this was pulled out.

    Business names the team rather than the person: a commercial conversation
    should not read as a queue with a named handler. "Enquiry" rather than
    "request" because that is what the Business front door calls it, and one
    thing should not have two names between one message and the next.

    Returns None when there is nobody to name and no team wording to fall back
    on - saying "someone" would be worse than the silence.
    """
    if item.department is Department.BUSINESS:
        return (
            f"{MARK_OWNER} <b>Enquiry {_e(item.client_reference)}</b> — Our "
            f"{_e(item.department.label)} Team is looking into your enquiry."
        )
    if actor_name:
        return (
            f"{MARK_OWNER} <b>Request {_e(item.client_reference)}</b> — "
            f"{_e(actor_name)} is now looking after your request."
        )
    return None


async def open_request(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    source_chat: Chat,
    subject: str,
    body: str,
    raised_by_name: str,
    raised_by_telegram_user_id: int | None = None,
    original_telegram_message_id: int | None = None,
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

    # The client's own words, recorded as a message rather than only as a
    # column on the work item.
    #
    # `original_message` on the work item is what the header and the closure
    # notice quote, and for a long time it was the only place the opening
    # message existed. That was invisible until the archive was opened on 18
    # September: the archive forwards `Message` rows, so every archived ticket
    # held our four outbound messages and none of the client's - the one thing
    # the forwarding was chosen for. An archive that cannot show what the
    # client actually asked for cannot settle a dispute, which is the entire
    # reason NexterPay wanted forwards rather than copies.
    #
    # Recorded first, so it sorts ahead of the header and the acknowledgement
    # and the archived conversation opens the way the real one did. Recorded
    # against the client's group and their real message id, so the forward
    # carries their name and their timestamp rather than the bot's.
    #
    # This does not change reply routing. Inbound client messages in the client
    # group are already anchors - `add_to_request` has recorded follow-ups this
    # way all along - so the opening message simply becomes the first of them
    # instead of the only one missing.
    if original_telegram_message_id is not None:
        await _record_message(
            session, item,
            direction=MessageDirection.INBOUND,
            chat_id=source_chat.telegram_chat_id,
            message_id=original_telegram_message_id,
            sender_name=raised_by_name,
            sender_telegram_user_id=raised_by_telegram_user_id,
            text=body,
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
        parse_mode="HTML",
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
    from_chat: Chat | None = None,
    topic_keyboard=None,
) -> None:
    """Counterparty → topic. Reopens nothing and changes no status; staff decide.

    `from_chat` says which side spoke, and matters only on a two-sided request.
    Getting it wrong would be worse than cosmetic: the message is recorded
    against that chat, and the reply-to-acknowledgement strategy resolves later
    replies by looking the id up in that chat. Recorded against the wrong one,
    a supplier's follow-up would resolve to nothing and be silently dropped -
    the failure this platform has already had twice.
    """
    source, ops = await chats_for(session, item)
    source = from_chat or source

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

        # Which side spoke, but only when there are two of them.
        #
        # On a two-sided request the team reads one conversation with a client
        # at one end and a supplier at the other, and "Tom has replied" is
        # ambiguous in a way that matters: the answer to it goes back out to
        # somebody, and to the wrong somebody if the reader guessed. A
        # one-sided request has nothing to disambiguate, so it says nothing.
        side = ""
        if item.bridged_chat_id is not None:
            side = f" <i>({html.escape(source.title or 'other side')})</i>"

        who = (
            f"<b>{html.escape(sender_name)} has replied</b>{side} on "
            f"{item.display_reference}"
        )
        if owner is not None:
            body = f"{mention_for(owner)} — {who}{context}\n{quoted}"
        else:
            body = f"{who}{context}\n{quoted}"
        # The keyboard is passed in rather than built here, for the same reason
        # `ack_keyboard` is: this module must not import the bot layer. It goes
        # on the client's words themselves so that answering is a tap from the
        # thing being answered, rather than a scroll back to the action row.
        await gateway.send_message(
            ops.telegram_chat_id, body, thread_id=item.topic_id, parse_mode="HTML",
            reply_markup=topic_keyboard,
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
        # The reference this side is shown, never the other side's.
        #
        # This said `item.client_reference` until 20 September, which is right
        # for a one-sided request and hands the supplier the client's code on a
        # two-sided one - `source` is reassigned to `from_chat` above, so this
        # message goes to whoever wrote and was built from whoever raised.
        #
        # The same fault as 16 September, in a path that was not looked at when
        # that one was fixed: `reference_for` was written, applied to replies
        # and closures, and this notice was left composing its own. It is the
        # third time the words have stayed inside and the reference has not.
        shown_reference = await reference_for(session, item, source)
        note = (
            f"{shown_reference} is already closed, so this has been passed to "
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
    to_chat: Chat | None = None,
    origin_message_id: int | None = None,
) -> None:
    """The only path from NexterPay to a counterparty.

    `to_chat` names the destination, and matters only on a two-sided request.
    Left out, the reply goes to the group the request was raised in, which is
    what every one-sided request has always done.

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

    # Which outside group this is going to, checked rather than trusted.
    #
    # This is the explicit guarantee that replaced an implicit one. Until
    # two-sided tickets existed, a request had exactly one outside group and
    # there was no code path that could send to the wrong party. Now there can
    # be two, so the destination is named by the caller - and refused here if
    # it is not a party to this request.
    #
    # Checked in this function rather than at the call site on purpose. A rule
    # enforced by every caller is a rule enforced until somebody writes a new
    # caller.
    allowed = await counterparty_chats(session, item)
    source = to_chat or source
    if source.id not in {chat.id for chat in allowed}:
        raise DomainError(
            f"{source.title or source.telegram_chat_id} is not a party to "
            f"{item.display_reference}, so nothing was sent."
        )

    # The reference this particular side is shown, never the other side's.
    #
    # This was `item.client_reference` until 16 September, which is correct for
    # a one-sided request and hands the supplier the client's code on a
    # two-sided one. Never `display_reference`, which carries both.
    shown_reference = await reference_for(session, item, source)
    outbound = staff_reply_text(shown_reference, text, sender=actor.name)

    # HTML on every path from 20 September, not only when a contact is tagged.
    parse_mode = "HTML"
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
            outbound = staff_reply_text(
                shown_reference, text,
                sender=actor.name, mention=named, escape=True,
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
        origin_message_id=origin_message_id,
    )

    if attachment is not None:
        file_msg = await gateway.send_file(
            source.telegram_chat_id, attachment.file_id, attachment.kind
        )
        await _store_attachment(session, item, attachment, source.telegram_chat_id,
                                file_msg.message_id, MessageDirection.OUTBOUND, actor.name)

    # Who it went to, recorded on the event itself.
    #
    # The history line read "Reply sent to client by peter" whichever side it
    # went to, which on a two-sided request is not a wording problem - it is the
    # audit trail saying something untrue. Section 4 asks for the direction of
    # every message to be recorded, and a line that names the wrong party is
    # worse than one that names none.
    event = await wi.record_event(
        session, item, EventType.STAFF_REPLY_SENT, actor,
        text=outbound[:500],
        to=source.title if item.bridged_chat_id is not None else None,
    )
    await announce(session, gateway, item, event)


# Telegram will not delete a message more than 48 hours old, for anybody.
#
# From the Bot API's own list of limitations on deleteMessage: "A message can
# only be deleted if it was sent less than 48 hours ago." Nothing on our side
# changes that, so a retraction past the window edits the message instead of
# removing it - which is the honest outcome, because the counterparty has
# certainly read it by then and pretending otherwise would be a worse lie than
# the correction.
RETRACTION_WINDOW = timedelta(hours=48)

RETRACTED_TEXT = "This message was withdrawn by NexterPay."


def _as_utc(value: datetime) -> datetime:
    """A stored timestamp, made safe to subtract.

    The column is `DateTime(timezone=True)` and Postgres honours that. SQLite
    does not — it hands back a naive datetime, and subtracting one from an
    aware one raises rather than quietly giving a wrong answer, which is the
    one mercy in it.

    Tests run on SQLite and production is Postgres, so this is exactly the
    class of fault the suite is structurally placed to catch late: it passed
    ruff, it passed review, and it failed on the first test that did real
    arithmetic on a stored time.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def relayed_copies_of(
    session: AsyncSession, origin_message_id: int
) -> list[Message]:
    """The counterparty-facing messages a given Operations message produced.

    A list rather than one, because a reply on a two-sided request can be sent
    to each side separately and both came from the same composition. Correcting
    one and leaving the other would be worse than correcting neither.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.origin_message_id == origin_message_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.telegram_message_id.is_not(None),
        )
        .order_by(Message.id)
    )
    return list(result.scalars().all())


async def edit_relayed_reply(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    origin_message_id: int,
    new_text: str,
) -> int:
    """Somebody corrected what they sent. Correct what the counterparty sees.

    NexterPay, 19 September: "the message gets edited internally but on client
    group message send out remains the same". They were right, and there was no
    mechanism at all - nothing in this platform had ever looked at an
    `edited_message` update.

    Returns how many copies were corrected, which is zero for anything sent
    before 20 September. Those have no `origin_message_id`, so there is nothing
    to find. Saying so is better than silently doing nothing.

    The header is rebuilt rather than patched, so a corrected message is shaped
    exactly like a fresh one - same marker, same reference, same invitation.
    """
    copies = await relayed_copies_of(session, origin_message_id)
    if not copies:
        return 0

    corrected = 0
    for copy in copies:
        chat = await _chat_by_telegram_id(session, copy.telegram_chat_id)
        reference = (
            await reference_for(session, item, chat)
            if chat is not None
            else item.client_reference
        )
        rebuilt = staff_reply_text(reference, new_text, sender=copy.sender_name)
        try:
            await gateway.edit_message_text(
                copy.telegram_chat_id, copy.telegram_message_id, rebuilt,
                parse_mode="HTML",
            )
        except Exception:
            # One group refusing an edit must not stop the other side being
            # corrected. Telegram refuses an edit that changes nothing, which
            # is harmless and common - somebody fixing whitespace.
            logger.exception(
                "Could not edit the copy of %s in chat %s",
                item.display_reference, copy.telegram_chat_id,
            )
            continue
        copy.text = rebuilt
        corrected += 1

    await session.flush()
    return corrected


async def retract_relayed_reply(
    session: AsyncSession,
    gateway: TelegramGateway,
    item: WorkItem,
    origin_message_id: int,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Take a sent message back. Returns (deleted, withdrawn).

    The answer to NexterPay's second point, and deliberately not the thing they
    asked for. They asked for a deletion in the topic to remove the
    counterparty's copy; Telegram never tells a bot that a message was deleted
    in a group, so there is no event to act on and no amount of work produces
    one. An explicit button is the honest version: it always knows it was
    pressed.

    Two outcomes, because Telegram allows a delete only inside 48 hours.
    Younger than that, the copy goes. Older, it is edited to say it was
    withdrawn, because it cannot be removed and leaving it unmarked would be
    the same as doing nothing.
    """
    copies = await relayed_copies_of(session, origin_message_id)
    now = now or utcnow()

    deleted = withdrawn = 0
    for copy in copies:
        within_window = (now - _as_utc(copy.sent_at)) < RETRACTION_WINDOW
        try:
            if within_window:
                await gateway.delete_message(
                    copy.telegram_chat_id, copy.telegram_message_id
                )
                deleted += 1
            else:
                await gateway.edit_message_text(
                    copy.telegram_chat_id, copy.telegram_message_id, RETRACTED_TEXT
                )
                withdrawn += 1
        except Exception:
            logger.exception(
                "Could not retract the copy of %s in chat %s",
                item.display_reference, copy.telegram_chat_id,
            )
            continue
        copy.text = RETRACTED_TEXT

    await session.flush()
    return deleted, withdrawn


async def _chat_by_telegram_id(
    session: AsyncSession, telegram_chat_id: int
) -> Chat | None:
    result = await session.execute(
        select(Chat).where(Chat.telegram_chat_id == telegram_chat_id)
    )
    return result.scalar_one_or_none()


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
    """Take ownership, and tell the counterparty who now has it.

    NexterPay, 5 September: "if it is claimed, the client should know who they
    are speaking with, more personal". Until now the client saw a request
    acknowledged and then silence until somebody replied, with no sign anyone
    had picked it up.

    The name is the staff member's display name from their record rather than
    their Telegram name, so NexterPay decide what a counterparty sees.

    Business is told the same thing without the name. NexterPay's wording, and
    their reasoning is sound: a commercial conversation should not read as a
    queue with a named handler, but silence was worse than either. The client
    still learns somebody has picked it up.

    The wording itself lives in `claim_notice_text`, where it can be read
    without a database.

    Replies stay signed everywhere, Business included. A negotiation is the
    most personal conversation on the platform; it is the claim notice that
    reads as process, not the answer.
    """
    before = await _last_event_id(session, item)
    await wi.claim(session, item, actor)
    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)

    who = claim_notice_text(item, actor.name)
    if who is None:
        return

    source, _ = await chats_for(session, item)
    # `claim_notice_text` owns the whole line, reference included.
    #
    # This used to prefix it here, which was harmless while the notice was a
    # bare sentence and became wrong the moment it started with a marker: the
    # client got "#1000 — 👤 Request #1000 — Sarah Hill is now…", the symbol
    # buried mid-string and the reference twice.
    notice = who
    sent = await gateway.send_message(
        source.telegram_chat_id, notice, parse_mode="HTML"
    )
    await _record_message(
        session, item,
        direction=MessageDirection.OUTBOUND,
        chat_id=source.telegram_chat_id,
        message_id=sent.message_id,
        sender_name="NexterPay Operations",
        text=notice,
    )


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
    """Put a closed request back into play. Manager and above.

    Two routes out, depending on whether the archive has already taken it.
    Before archiving existed there was only one, and reopening an archived
    request would have tried to write into a topic that had been deleted -
    which fails, and fails in a way that reads as the bot being broken rather
    than as the ticket having moved.
    """
    if item.status is not WorkItemStatus.CLOSED:
        return

    _, ops = await chats_for(session, item)
    before = await _last_event_id(session, item)
    await wi.reopen(session, item, actor)

    if item.archived_at is not None:
        await _reopen_from_archive(session, gateway, item, ops)
    elif item.topic_id is not None:
        await gateway.reopen_topic(ops.telegram_chat_id, item.topic_id)

    await _announce_since(session, gateway, item, before)
    await refresh_header(session, gateway, item)


def archive_link(archive_chat_id: int, thread_id: int) -> str | None:
    """A tappable link to a topic in a private supergroup.

    Telegram builds these from the chat id with the -100 prefix stripped. Any
    other shape is not a supergroup and has no such link, so this returns None
    rather than composing something that would 404 - a dead link in an
    Operations topic is worse than a sentence saying where to look.
    """
    raw = str(archive_chat_id)
    if not raw.startswith("-100"):
        return None
    return f"https://t.me/c/{raw[4:]}/{thread_id}"


async def _reopen_from_archive(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem, ops: Chat
) -> None:
    """A fresh topic for a request the archive has already taken.

    NexterPay, 9 September: "If reopened, a new active topic would be created
    and linked back to the archived ticket."

    The archived copy is deliberately left where it is. It is read-only and it
    is the record of how the ticket finished the first time; deleting it to
    make the reopened one the single version would destroy the thing the
    archive exists for.

    The link to it goes in the new topic as a message rather than in a column,
    because `archived_at` is cleared here - the request is live again, and if
    it is closed a second time it has to archive again rather than be skipped
    by a sweep that thinks it has already been done.
    """
    # Imported here rather than at module scope: `archive` imports this module,
    # so a top-level import would be circular.
    from app.services.archive import archive_chat_for

    was_topic = item.archive_topic_id
    archive_chat = await archive_chat_for(session, item.department)

    client = await session.get(Client, item.client_id)
    item.topic_id = await gateway.create_topic(
        ops.telegram_chat_id,
        topic_name(item, client.name if client else "Unknown client"),
    )
    item.header_message_id = None
    item.archived_at = None
    item.archive_topic_id = None
    await session.flush()

    header = await gateway.send_message(
        ops.telegram_chat_id,
        header_text(item, client.name if client else "Unknown client"),
        thread_id=item.topic_id,
        parse_mode="HTML",
    )
    item.header_message_id = header.message_id
    await session.flush()

    link = (
        archive_link(archive_chat.telegram_chat_id, was_topic)
        if archive_chat is not None and was_topic is not None
        else None
    )
    await gateway.send_message(
        ops.telegram_chat_id,
        "Reopened. The original topic was archived and removed, so this is a "
        "new one — the archived copy is still there and stays read-only."
        + (f"\n\n{link}" if link else ""),
        thread_id=item.topic_id,
    )


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


def outbound_body(subject: str, body: str) -> str:
    """The part of an outbound message that follows the subject line.

    When a member of staff raises something outbound, the subject is taken
    from the first line of what they typed. Printing the subject line and then
    the whole body therefore repeats that line - and when they typed a single
    line, repeats the entire message.

    NexterPay's tester, 12 September: "See the double message". He was looking
    at the preview; the same duplication was in the message the counterparty
    received, which is the half that mattered.

    Only drops the line when it really is the subject. `open_outbound` can be
    called with a subject that did not come from the body, and a subject longer
    than 120 characters is truncated - in both cases the body is shown whole,
    because repeating a line is a much smaller fault than silently eating one.
    """
    lines = body.splitlines()
    first = lines[0].strip() if lines else ""
    if first and first == (subject or "").strip():
        return "\n".join(lines[1:]).strip()
    return body.strip()


def outbound_opening_text(item: WorkItem, body: str) -> str:
    """What the counterparty receives when NexterPay raise something with them.

    Deliberately not the acknowledgement wording. "Request X has been logged
    with our Support team" is nonsense when we are the ones raising it.
    """
    parts = [f"{item.client_reference} · {item.subject}"]
    rest = outbound_body(item.subject or "", body)
    if rest:
        parts.append(rest)
    parts.append("Reply to this message to respond.")
    return "\n\n".join(parts)


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
        # Both sides, on a two-sided request. NexterPay, 16 September, asked
        # directly whether the supplier should be told when one closes: "Tell
        # Both".
        #
        # Each is told using their own reference. A supplier reading the
        # client's code here would be the same leak as in a reply, arriving by
        # a different door - which is exactly how the first one arrived.
        for chat in await counterparty_chats(session, item):
            reference = await reference_for(session, item, chat)
            text = closure_text(
                item, resolution, reference,
                # Only the side that raised it is shown what was raised.
                raised_it=chat.id == item.source_chat_id,
            )
            sent = await gateway.send_message(
                chat.telegram_chat_id, text, parse_mode="HTML"
            )
            await _record_message(
                session, item,
                direction=MessageDirection.OUTBOUND,
                chat_id=chat.telegram_chat_id,
                message_id=sent.message_id,
                sender_name="NexterPay Operations",
                text=text,
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
