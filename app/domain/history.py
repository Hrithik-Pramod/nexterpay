"""Rendering the event log as human-readable history.

This module exists because of a specific client decision. NexterPay will not
have any interface onto the platform's data; instead a NexterPay-controlled
Telegram account sits passively in the groups, and if anyone needs to retrace
what happened they log in and read the group themselves.

That only works if every ownership, status and priority change is *visible* in
the topic rather than merely recorded in the database. So each event renders to
a line of text, and the same renderer serves both purposes:

  * the bot posts `render_event(...)` into the topic as changes happen, and
  * `render_history(...)` reconstructs a full trail from the event log alone.

If you add an EventType, add a renderer for it here. The test suite fails
otherwise, deliberately.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, WorkItem
from app.domain.enums import EventType


async def load_events(session: AsyncSession, work_item: WorkItem) -> list[Event]:
    """Fetch a work item's events in the order they occurred.

    Explicit rather than lazy-loaded, so callers never trigger IO by touching
    an attribute.
    """
    result = await session.execute(
        select(Event).where(Event.work_item_id == work_item.id).order_by(Event.id)
    )
    return list(result.scalars().all())


def _quote(text: str | None, limit: int = 160) -> str:
    """Attach the words to the line, on one line, or nothing at all.

    NexterPay have no interface onto this data - a passive account reads the
    group. So this history IS the audit trail, and "Internal note by peter"
    without the note is not an audit trail. Newlines are flattened because a
    history entry has to stay one entry.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return f': "{flat}"'


def _fmt(event: Event, *, verbose: bool = False) -> str:
    p = event.payload or {}
    actor = event.actor_name or "System"
    t = event.event_type

    if t is EventType.WORK_ITEM_CREATED:
        return f"Work Item created by {actor} ({p.get('client', 'unknown client')})"
    if t is EventType.TOPIC_CREATED:
        return "Topic opened"
    if t is EventType.OWNERSHIP_CLAIMED:
        return f"Claimed by {actor}"
    if t is EventType.OWNERSHIP_ASSIGNED:
        return f"Assigned to {p.get('assignee', 'unknown')} by {actor}"
    if t is EventType.OWNERSHIP_RELEASED:
        return f"Ownership released by {actor}"
    if t is EventType.STATUS_CHANGED:
        return f"Status: {p.get('from_label')} → {p.get('to_label')} ({actor})"
    if t is EventType.PRIORITY_CHANGED:
        return f"Priority: {p.get('from_label')} → {p.get('to_label')} ({actor})"
    if t is EventType.INTERNAL_NOTE_ADDED:
        return f"Internal note by {actor}" + (_quote(p.get("note")) if verbose else "")
    if t is EventType.CLIENT_MESSAGE_RECEIVED:
        return f"Message received from {actor}" + (_quote(p.get("text")) if verbose else "")
    if t is EventType.STAFF_REPLY_SENT:
        return f"Reply sent to client by {actor}" + (_quote(p.get("text")) if verbose else "")
    if t is EventType.ATTACHMENT_RECEIVED:
        return f"Attachment received from {actor} ({p.get('file_name') or p.get('kind', 'file')})"
    if t is EventType.SUPPLIER_FILED:
        supplier = p.get("supplier") or "unknown"
        was = p.get("from_reference")
        now = p.get("to_reference")
        if was and now:
            return f"Filed under {supplier} by {actor} ({was} → {now})"
        return f"Filed under {supplier} by {actor}"
    if t is EventType.WORK_ITEM_CLOSED:
        return f"Closed by {actor}"
    if t is EventType.WORK_ITEM_REOPENED:
        return f"Reopened by {actor}"
    if t is EventType.TOPIC_CLOSED:
        return "Topic closed"

    raise NotImplementedError(f"No renderer for event type {t!r}")


def render_event(
    event: Event, *, with_timestamp: bool = False, verbose: bool = False
) -> str:
    """One line describing a single event.

    Posted into the Telegram topic as changes occur, where `verbose` stays off:
    the message itself is already sitting in the topic a line above, so quoting
    it back would just be noise. History is the opposite case - it is rebuilt
    from the event log alone, with nothing else to read - so it quotes.
    """
    body = _fmt(event, verbose=verbose)
    if with_timestamp:
        return f"[{event.created_at:%d %b %Y %H:%M}] {body}"
    return body


def render_history(events: Iterable[Event]) -> list[str]:
    """The full trail for a work item, oldest first, with timestamps."""
    return [render_event(e, with_timestamp=True, verbose=True) for e in events]
