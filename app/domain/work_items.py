"""Work item lifecycle.

The single rule this module enforces: **no work item changes without an event**.
Every mutating function records an Event before returning, and callers outside
this module must not modify WorkItem fields directly. That discipline is what
makes the audit trail trustworthy, and it is unrecoverable if it slips.

This layer knows nothing about Telegram. It takes and returns domain objects so
it can be tested without a bot, a token or a network.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Chat, Client, Event, ReferenceCounter, Staff, WorkItem
from app.domain.enums import (
    ChatKind,
    Department,
    EventType,
    Priority,
    StaffRole,
    WorkItemStatus,
)
from app.domain.errors import (
    AlreadyOwned,
    InvalidTransition,
    NotAuthorised,
    WorkItemClosed,
)

# Section 13 of the PRD. Operators may work items; the more disruptive actions
# require seniority.
ROLE_REQUIRED_TO_REASSIGN = StaffRole.SENIOR_OPERATOR
ROLE_REQUIRED_TO_CHANGE_PRIORITY = StaffRole.SENIOR_OPERATOR
ROLE_REQUIRED_TO_ESCALATE = StaffRole.SENIOR_OPERATOR
ROLE_REQUIRED_TO_REOPEN = StaffRole.MANAGER


@dataclass(frozen=True)
class Actor:
    """Who performed an action. `staff` is None for clients and the system."""

    name: str
    staff: Staff | None = None
    telegram_user_id: int | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(name="System")

    @classmethod
    def of(cls, staff: Staff) -> Actor:
        return cls(name=staff.display_name, staff=staff, telegram_user_id=staff.telegram_user_id)

    def require_any(self) -> Staff:
        """Any active staff member. Used where the action carries no privilege
        beyond being a member of the team - replying to a client, for example."""
        return self.require(StaffRole.OPERATOR)

    def require(self, role: StaffRole) -> Staff:
        if self.staff is None or not self.staff.is_active:
            raise NotAuthorised("Action requires an active staff account")
        if not self.staff.role.at_least(role):
            raise NotAuthorised(
                f"Action requires {role.value}; "
                f"{self.staff.display_name} is {self.staff.role.value}"
            )
        return self.staff


async def record_event(
    session: AsyncSession,
    work_item: WorkItem,
    event_type: EventType,
    actor: Actor,
    **payload,
) -> Event:
    """Append to the audit log. The only way events are created.

    Uses the foreign key rather than the relationship so that recording an
    event never triggers a lazy load of the work item's existing events.
    """
    event = Event(
        work_item_id=work_item.id,
        event_type=event_type,
        actor_staff_id=actor.staff.id if actor.staff else None,
        actor_telegram_user_id=actor.telegram_user_id,
        actor_name=actor.name,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event


async def _next_reference(session: AsyncSession) -> int:
    counter = await session.get(ReferenceCounter, 1, with_for_update=True)
    if counter is None:
        counter = ReferenceCounter(id=1, next_value=1000)
        session.add(counter)
        await session.flush()
    value = counter.next_value
    counter.next_value = value + 1
    await session.flush()
    return value


async def operations_chat_for(session: AsyncSession, department: Department) -> Chat:
    result = await session.execute(
        select(Chat).where(
            Chat.kind == ChatKind.OPERATIONS,
            Chat.department == department,
            Chat.is_active.is_(True),
        )
    )
    chat = result.scalar_one_or_none()
    if chat is None:
        raise LookupError(f"No Operations Group registered for {department.value}")
    return chat


async def create_work_item(
    session: AsyncSession,
    *,
    source_chat: Chat,
    subject: str,
    original_message: str,
    raised_by_name: str,
    raised_by_telegram_user_id: int | None = None,
    priority: Priority = Priority.MEDIUM,
) -> WorkItem:
    """Create a work item from a client request.

    Routing is by source group alone (PRD 8.2) - no manual triage.
    """
    if source_chat.kind is not ChatKind.CLIENT:
        raise ValueError("Work items originate from client groups only")
    if source_chat.client_id is None:
        raise ValueError("Client group is not linked to a client")

    ops_chat = await operations_chat_for(session, source_chat.department)
    client = await session.get(Client, source_chat.client_id)

    work_item = WorkItem(
        reference=await _next_reference(session),
        client_id=source_chat.client_id,
        department=source_chat.department,
        source_chat_id=source_chat.id,
        operations_chat_id=ops_chat.id,
        raised_by_name=raised_by_name,
        raised_by_telegram_user_id=raised_by_telegram_user_id,
        subject=subject.strip()[:300],
        original_message=original_message,
        status=WorkItemStatus.OPEN,
        priority=priority,
    )
    session.add(work_item)
    await session.flush()

    await record_event(
        session,
        work_item,
        EventType.WORK_ITEM_CREATED,
        Actor(name=raised_by_name, telegram_user_id=raised_by_telegram_user_id),
        client=client.name if client else None,
        department=source_chat.department.value,
        subject=work_item.subject,
    )
    return work_item


async def attach_topic(
    session: AsyncSession, work_item: WorkItem, topic_id: int, actor: Actor | None = None
) -> WorkItem:
    work_item.topic_id = topic_id
    await record_event(
        session, work_item, EventType.TOPIC_CREATED, actor or Actor.system(), topic_id=topic_id
    )
    return work_item


def _guard_open(work_item: WorkItem) -> None:
    if work_item.status is WorkItemStatus.CLOSED:
        raise WorkItemClosed(f"{work_item.display_reference} is closed")


async def claim(session: AsyncSession, work_item: WorkItem, actor: Actor) -> WorkItem:
    _guard_open(work_item)
    staff = actor.require(StaffRole.OPERATOR)

    if work_item.owner_staff_id is not None and work_item.owner_staff_id != staff.id:
        raise AlreadyOwned(f"{work_item.display_reference} is already owned")

    work_item.owner_staff_id = staff.id
    await record_event(session, work_item, EventType.OWNERSHIP_CLAIMED, actor)

    # Claiming means starting, so the status follows in the same action rather
    # than waiting for a second tap nobody remembers to make. The transition is
    # recorded as its own event: the history has to show WHY the status moved,
    # and "peter claimed it" and "it became In Progress" are two facts.
    if work_item.status is WorkItemStatus.OPEN:
        previous = work_item.status
        work_item.status = WorkItemStatus.IN_PROGRESS
        await record_event(
            session,
            work_item,
            EventType.STATUS_CHANGED,
            actor,
            from_value=previous.value,
            to_value=WorkItemStatus.IN_PROGRESS.value,
            from_label=previous.label,
            to_label=WorkItemStatus.IN_PROGRESS.label,
        )
    return work_item


async def assign(
    session: AsyncSession, work_item: WorkItem, assignee: Staff, actor: Actor
) -> WorkItem:
    """Reassign ownership. Senior Operator and above (PRD 13)."""
    _guard_open(work_item)
    actor.require(ROLE_REQUIRED_TO_REASSIGN)

    if not assignee.is_active:
        raise NotAuthorised("Cannot assign to an inactive staff member")

    work_item.owner_staff_id = assignee.id
    await record_event(
        session, work_item, EventType.OWNERSHIP_ASSIGNED, actor, assignee=assignee.display_name
    )

    # Same reasoning as claim(): giving someone a request starts it. Keeping
    # assign() and claim() on different status rules would mean the header said
    # something different depending on how ownership happened to be set.
    if work_item.status is WorkItemStatus.OPEN:
        previous = work_item.status
        work_item.status = WorkItemStatus.IN_PROGRESS
        await record_event(
            session,
            work_item,
            EventType.STATUS_CHANGED,
            actor,
            from_value=previous.value,
            to_value=WorkItemStatus.IN_PROGRESS.value,
            from_label=previous.label,
            to_label=WorkItemStatus.IN_PROGRESS.label,
        )
    return work_item


async def change_status(
    session: AsyncSession, work_item: WorkItem, new_status: WorkItemStatus, actor: Actor
) -> WorkItem:
    _guard_open(work_item)

    if new_status is WorkItemStatus.ESCALATED:
        actor.require(ROLE_REQUIRED_TO_ESCALATE)
    else:
        actor.require(StaffRole.OPERATOR)

    if new_status is WorkItemStatus.CLOSED:
        raise InvalidTransition("Use close() to close a work item")
    if new_status is work_item.status:
        return work_item

    previous = work_item.status
    work_item.status = new_status
    await record_event(
        session,
        work_item,
        EventType.STATUS_CHANGED,
        actor,
        from_value=previous.value,
        to_value=new_status.value,
        from_label=previous.label,
        to_label=new_status.label,
    )
    return work_item


async def change_priority(
    session: AsyncSession, work_item: WorkItem, new_priority: Priority, actor: Actor
) -> WorkItem:
    _guard_open(work_item)
    actor.require(ROLE_REQUIRED_TO_CHANGE_PRIORITY)

    if new_priority is work_item.priority:
        return work_item

    previous = work_item.priority
    work_item.priority = new_priority
    await record_event(
        session,
        work_item,
        EventType.PRIORITY_CHANGED,
        actor,
        from_value=previous.value,
        to_value=new_priority.value,
        from_label=previous.label,
        to_label=new_priority.label,
    )
    return work_item


async def close(session: AsyncSession, work_item: WorkItem, actor: Actor) -> WorkItem:
    if work_item.status is WorkItemStatus.CLOSED:
        return work_item
    actor.require(StaffRole.OPERATOR)

    work_item.status = WorkItemStatus.CLOSED
    work_item.closed_at = utcnow()
    await record_event(session, work_item, EventType.WORK_ITEM_CLOSED, actor)
    return work_item


async def reopen(session: AsyncSession, work_item: WorkItem, actor: Actor) -> WorkItem:
    """Reopen a closed work item.

    Whether a client following up should reopen the original item or create a
    new one is still an open question with NexterPay (pre-start question B2).
    Both paths are supported; the bot layer decides which to call once the
    client confirms.
    """
    if work_item.status is not WorkItemStatus.CLOSED:
        return work_item
    actor.require(ROLE_REQUIRED_TO_REOPEN)

    work_item.status = WorkItemStatus.IN_PROGRESS
    work_item.closed_at = None
    await record_event(session, work_item, EventType.WORK_ITEM_REOPENED, actor)
    return work_item


async def open_items_for_chat(session: AsyncSession, chat: Chat) -> list[WorkItem]:
    """Open work items raised from a given client group, newest first."""
    result = await session.execute(
        select(WorkItem)
        .where(
            WorkItem.source_chat_id == chat.id,
            WorkItem.status.not_in([WorkItemStatus.CLOSED]),
        )
        .order_by(WorkItem.updated_at.desc())
    )
    return list(result.scalars().all())
