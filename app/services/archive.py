"""Moving finished work out of the desk's topic list.

NexterPay, 9 September, refined on the 14th:

    Could we instead run two forum groups: Active Tickets and Closed Tickets?
    On closure, the bot would create a matching topic in the Closed Tickets
    group and reconstruct the full ticket from our stored record... The
    archive topic would then be read-only. The original active topic could
    remain for 24 hours before deletion.

The reason is the topic list. A desk that has run for six months has several
hundred closed topics sitting above the live ones, and triage happens in that
list — so the cost of keeping finished work there is paid every time anybody
looks for something.

**Forwarded, not copied.** NexterPay chose that on the 12th and it is the
single decision this module is shaped around. A copy is the bot repeating
somebody else's words in its own voice; a forward carries the original author
and timestamp. An archive that attributes every line to the bot can answer
"what was said" and not "who said it", and the second is the question that
gets asked six weeks later by somebody holding a dispute.

Three properties worth stating, because each one is a way this could go wrong
quietly:

* It is **idempotent**. `archived_at` is set at the end, and the sweep only
  looks at rows where it is null. A crash halfway means the next pass redoes
  the ticket rather than skipping it — duplicated effort, never lost work.

* It **archives before it deletes**. The original topic is removed only after
  the copy exists and `archived_at` is written. The other order would, on a
  bad day, delete the only record.

* It is **quiet when unconfigured**. A desk with no archive group registered
  is not swept at all. Archiving is housekeeping; it must never be the reason
  the bot stops answering people — and, learned the hard way on 18 September,
  it must never let an unconfigured desk's backlog crowd a configured desk's
  work out of the batch. See `due_for_archive`.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Chat, Message, WorkItem
from app.domain.enums import ChatKind, Department, WorkItemStatus
from app.domain.history import load_events, render_history
from app.services.gateway import TelegramGateway
from app.services.relay import chats_for, status_symbol

logger = logging.getLogger(__name__)

# NexterPay, 14 September: "Lets do 24hours, if its closed it can go."
#
# The delay is not caution about the archive — that part is safe the moment it
# is closed. It is for the desk: somebody closes a ticket, the client replies
# an hour later saying it is not fixed, and reopening a topic that still exists
# is a different afternoon from reconstructing one that does not.
ARCHIVE_AFTER = timedelta(hours=24)

# How many tickets one sweep will move.
#
# Forwarding is rate-limited per chat, so a desk closing forty tickets in an
# afternoon would otherwise spend the next hour inside a single pass with the
# rest of the platform waiting behind it. Ten a sweep clears a backlog over a
# few hours and never blocks anything.
BATCH = 10


async def archive_chat_for(
    session: AsyncSession, department: Department
) -> Chat | None:
    """The archive group behind a desk, if one has been registered."""
    result = await session.execute(
        select(Chat).where(
            Chat.kind == ChatKind.ARCHIVE,
            Chat.department == department,
            Chat.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def archiving_departments(session: AsyncSession) -> list[Department]:
    """Desks that have somewhere to put finished work."""
    result = await session.execute(
        select(Chat.department).where(
            Chat.kind == ChatKind.ARCHIVE,
            Chat.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def due_for_archive(
    session: AsyncSession, *, now=None, limit: int = BATCH
) -> list[WorkItem]:
    """Closed long enough ago, not yet moved, and with somewhere to go.

    Ordered oldest first so a backlog drains in the order it accumulated,
    rather than the newest closures jumping the queue every sweep and the
    oldest never being reached at all.

    **Why the department filter is here and not left to `archive_one`.** It was
    left to `archive_one` at first, which returned False for a desk with no
    archive group and logged it. That reads as harmless - the ticket stays put,
    nothing is lost - and it starved the archive completely within a week.

    A ticket that cannot be archived never gets `archived_at`, so it is due
    again on the next sweep, and on every sweep after that. Ten such tickets is
    the whole batch. On 18 September the ten oldest closed tickets on this
    platform were all on desks that had never been given an archive group, and
    they had been taking all ten slots every fifteen minutes since - while
    Support, which *was* configured, had work closed the previous day sitting
    behind them that would never have been reached.

    So: nowhere to put it means not due. The filter belongs in the query that
    fills the batch, because the bug was never about archiving - it was about
    which tickets are allowed to occupy the batch.
    """
    departments = await archiving_departments(session)
    if not departments:
        return []

    cutoff = (now or utcnow()) - ARCHIVE_AFTER
    result = await session.execute(
        select(WorkItem)
        .where(
            WorkItem.department.in_(departments),
            WorkItem.status == WorkItemStatus.CLOSED,
            WorkItem.closed_at.is_not(None),
            WorkItem.closed_at <= cutoff,
            WorkItem.archived_at.is_(None),
        )
        .order_by(WorkItem.closed_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def _conversation(session: AsyncSession, item: WorkItem) -> list[Message]:
    """Every message with something real behind it, oldest first.

    Records with no `telegram_message_id` are skipped: an internal note taken
    outside a topic has no Telegram message to forward, and asking Telegram to
    forward nothing produces an error rather than a blank.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.work_item_id == item.id,
            Message.telegram_message_id.is_not(None),
        )
        .order_by(Message.id)
    )
    return list(result.scalars().all())


def summary_text(
    item: WorkItem, client_name: str, owner_name: str, history: list[str]
) -> str:
    """The first message in the archived topic.

    Everything NexterPay listed — owner, status history, resolution — in one
    place at the top, so the archive answers the common questions without
    anybody scrolling through the forwarded conversation underneath.

    Written here rather than reusing the live header on purpose. The header is
    a working document that gets edited as ownership and status change; this is
    a statement of how the ticket finished, and it never changes again.
    """
    closed = item.closed_at.strftime("%d %b %Y at %H:%M") if item.closed_at else "—"
    lines = [
        f"{status_symbol(item)} {item.display_reference} — archived",
        "",
        f"Client        {client_name}",
        f"Department    {item.department.label}",
        f"Raised by     {item.raised_by_name}",
        f"Owner         {owner_name}",
        f"Closed        {closed}",
        "",
        f"Subject       {item.subject}",
        "",
        "History",
    ]
    lines += [f"  {line}" for line in history] or ["  (none recorded)"]
    lines += [
        "",
        "The conversation follows, forwarded from the original topic. This "
        "topic is read-only.",
    ]
    return "\n".join(lines)[:4000]


async def archive_one(
    session: AsyncSession, gateway: TelegramGateway, item: WorkItem
) -> bool:
    """Move one finished ticket into its desk's archive.

    Returns False when there is nowhere to put it, which is a configuration
    state rather than a failure — the desk simply has no archive group yet, and
    the ticket stays where it is until one is registered.
    """
    archive = await archive_chat_for(session, item.department)
    if archive is None:
        logger.info(
            "No archive group for %s; leaving %s in place",
            item.department.value, item.display_reference,
        )
        return False

    source, ops = await chats_for(session, item)

    # Loaded explicitly. `chats_for` fetches the chat by primary key and leaves
    # its relationships untouched, so reading `source.client` here would lazy
    # load inside async code - which does not raise a helpful error, it raises
    # MissingGreenlet from somewhere unrelated. The same applies to the owner.
    await session.refresh(source, ["client"])
    client_name = source.client.name if source.client else "—"

    owner_name = "nobody"
    if item.owner_staff_id is not None:
        await session.refresh(item, ["owner"])
        if item.owner is not None:
            owner_name = item.owner.display_name

    thread_id = await gateway.create_topic(
        archive.telegram_chat_id,
        f"{item.display_reference} · {item.subject}"[:128],
    )
    history = render_history(await load_events(session, item))
    await gateway.send_message(
        archive.telegram_chat_id,
        summary_text(item, client_name, owner_name, history),
        thread_id=thread_id,
    )

    for message in await _conversation(session, item):
        await gateway.forward_message(
            archive.telegram_chat_id,
            from_chat_id=message.telegram_chat_id,
            message_id=message.telegram_message_id,
            thread_id=thread_id,
        )

    # Read-only, as NexterPay asked. Done before the original is touched, so a
    # failure here leaves two topics rather than none.
    await gateway.close_topic(archive.telegram_chat_id, thread_id)

    item.archive_topic_id = thread_id
    item.archived_at = utcnow()
    await session.flush()

    # Only now. Everything above can be repeated safely; this cannot be undone.
    if item.topic_id is not None:
        await gateway.delete_topic(ops.telegram_chat_id, item.topic_id)
        logger.info(
            "Archived %s to topic %s and removed the original",
            item.display_reference, thread_id,
        )
        # And forgotten, which matters more than it looks.
        #
        # Leaving the id behind would mean every path that posts into a topic
        # still holding a thread that no longer exists - a client replying to a
        # closed request weeks later, for instance, whose notice goes into the
        # topic. Those paths all already check for None; none of them check
        # whether the topic is still there, because until now it always was.
        item.topic_id = None
        await session.flush()

    return True


async def sweep(
    session: AsyncSession, gateway: TelegramGateway, *, now=None
) -> int:
    """One pass. Returns how many tickets were moved.

    Each ticket is handled on its own and a failure on one does not stop the
    rest: a single group whose permissions are wrong should not hold up every
    other desk's housekeeping, and the one that failed will be picked up again
    on the next pass because `archived_at` was never set.
    """
    moved = 0
    for item in await due_for_archive(session, now=now):
        try:
            if await archive_one(session, gateway, item):
                moved += 1
        except Exception:
            logger.exception(
                "Could not archive %s; leaving it for the next sweep",
                item.display_reference,
            )
    return moved
