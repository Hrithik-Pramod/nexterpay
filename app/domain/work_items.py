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
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    Chat,
    Client,
    Event,
    ReferenceCounter,
    Staff,
    WorkItem,
    WorkItemLink,
)
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
    DomainError,
    InvalidTransition,
    NotAuthorised,
    WorkItemClosed,
)

# Section 13 of the PRD. Operators may work items; the more disruptive actions
# require seniority.
ROLE_REQUIRED_TO_REASSIGN = StaffRole.SENIOR_OPERATOR
# Opened to everyone on 3 September at NexterPay's request. It was Senior
# Operator, on the reasoning that priority is a claim on other people's time.
# In practice the person who knows a request is urgent is the one holding it,
# and making them find a senior just to say so was costing more than the
# occasional over-promotion. Every change is still recorded against a name.
ROLE_REQUIRED_TO_CHANGE_PRIORITY = StaffRole.OPERATOR
ROLE_REQUIRED_TO_ESCALATE = StaffRole.SENIOR_OPERATOR
ROLE_REQUIRED_TO_REOPEN = StaffRole.MANAGER


@dataclass(frozen=True)
class Actor:
    """Who performed an action, and with what seniority.

    `staff` is None for clients and the system. `role` is the person's role
    *in the department they are acting in*, which is not a property of the
    person: someone can be a Manager on their own desk and an Operator on the
    one they help out with. Carrying the role here rather than reading it off
    the Staff record is what stops Support seniority applying inside
    Compliance.
    """

    name: str
    staff: Staff | None = None
    telegram_user_id: int | None = None
    role: StaffRole | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(name="System")

    @classmethod
    def of(cls, staff: Staff, department: Department | None = None) -> Actor:
        """The person acting on one desk.

        The department may be left out only when there is no ambiguity - if
        someone belongs to exactly one, that is necessarily the one they are
        acting in. Once they span two it becomes a question with no answer,
        so this raises rather than picking. Picking wrong would either grant
        seniority they do not have on that desk or refuse them work they do.
        """
        if department is not None:
            role = staff.role_in(department)
        elif len(staff.memberships) == 1:
            role = staff.memberships[0].role
        else:
            raise ValueError(
                f"{staff.display_name} belongs to "
                f"{len(staff.memberships)} departments; say which one this is."
            )
        return cls(
            name=staff.display_name,
            staff=staff,
            telegram_user_id=staff.telegram_user_id,
            role=role,
        )

    def require_any(self) -> Staff:
        """Any active staff member. Used where the action carries no privilege
        beyond being a member of the team - replying to a client, for example."""
        return self.require(StaffRole.OPERATOR)

    def require(self, role: StaffRole) -> Staff:
        if self.staff is None or not self.staff.is_active:
            raise NotAuthorised("Action requires an active staff account")
        if self.role is None:
            # Registered, but not on this desk. Distinct from having too
            # junior a role, and worth saying so - the fix is different.
            raise NotAuthorised(
                f"{self.staff.display_name} is not registered for this department"
            )
        if not self.role.at_least(role):
            # "here" is doing the important work in this sentence, and it was
            # doing it too quietly. Somebody who is an administrator on one
            # desk reads "requires manager" and reasonably concludes the bot
            # is wrong about them, because /npwhoami has just told them
            # administrators are not limited to one department. Both are true:
            # administration is not limited, seniority is. The refusal has to
            # say which of the two it is talking about.
            def _plain(value: str) -> str:
                return value.replace("_", " ")

            raise NotAuthorised(
                f"That needs {_plain(role.value)} on this desk. "
                f"{self.staff.display_name} is {_plain(self.role.value)} here. "
                f"Seniority is held per department and does not carry across, "
                f"so a role on another desk does not apply in this group."
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
    department: Department | None = None,
) -> WorkItem:
    """Create a work item from a client request.

    Routing is by source group alone (PRD 8.2) - no manual triage. The one
    exception is `department`, used when NexterPay ask another desk to look at
    something on a client's behalf: the client and the reference stay the
    same, and only the Operations Group it lands in changes. Nothing about
    that reaches the client, which is why the caller is `open_internal` and
    not anything that writes outward.
    """
    if source_chat.kind is not ChatKind.CLIENT:
        raise ValueError("Work items originate from client groups only")
    if source_chat.client_id is None:
        raise ValueError("Client group is not linked to a client")

    department = department or source_chat.department
    ops_chat = await operations_chat_for(session, department)
    client = await session.get(Client, source_chat.client_id)

    work_item = WorkItem(
        reference=await _next_reference(session),
        client_id=source_chat.client_id,
        department=department,
        source_chat_id=source_chat.id,
        operations_chat_id=ops_chat.id,
        raised_by_name=raised_by_name,
        raised_by_telegram_user_id=raised_by_telegram_user_id,
        # Copied, not looked up later - see the note on the column.
        client_code=client.code if client else None,
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


def parse_reference(text: str) -> int | None:
    """Pull the ticket number out of whatever form someone typed.

    All of ACME-SPEX-1042, ACME-1042, #1042 and 1042 name the same ticket, and
    a person copying one out of a topic title, an email or a colleague's
    message will produce any of them. The number at the end is the unique key,
    so everything before it can be ignored. Being fussy here would only mean
    refusing references that are perfectly clear.
    """
    tail = (text or "").strip().rsplit("-", 1)[-1].lstrip("#").strip()
    return int(tail) if tail.isdigit() else None


async def by_reference(session: AsyncSession, reference: int) -> WorkItem | None:
    result = await session.execute(
        select(WorkItem).where(WorkItem.reference == reference)
    )
    return result.scalar_one_or_none()


def _pair(a: WorkItem, b: WorkItem) -> tuple[int, int]:
    return (a.id, b.id) if a.id < b.id else (b.id, a.id)


async def existing_link(
    session: AsyncSession, a: WorkItem, b: WorkItem
) -> WorkItemLink | None:
    lower, higher = _pair(a, b)
    result = await session.execute(
        select(WorkItemLink).where(
            WorkItemLink.lower_work_item_id == lower,
            WorkItemLink.higher_work_item_id == higher,
        )
    )
    return result.scalar_one_or_none()


async def linked_to(session: AsyncSession, work_item: WorkItem) -> list[WorkItem]:
    """The tickets connected to this one, in the order they were linked.

    A link row names two tickets and this one is on an unpredictable side of
    it, so the query fetches rows matching either side and then picks whichever
    id is not ours.
    """
    result = await session.execute(
        select(WorkItemLink)
        .where(
            (WorkItemLink.lower_work_item_id == work_item.id)
            | (WorkItemLink.higher_work_item_id == work_item.id)
        )
        .order_by(WorkItemLink.id)
    )
    others = [
        link.higher_work_item_id
        if link.lower_work_item_id == work_item.id
        else link.lower_work_item_id
        for link in result.scalars().all()
    ]
    if not others:
        return []

    found = await session.execute(select(WorkItem).where(WorkItem.id.in_(others)))
    by_id = {item.id: item for item in found.scalars().all()}
    # Reordered to match the link order, which `IN` does not preserve.
    return [by_id[i] for i in others if i in by_id]


async def link_tickets(
    session: AsyncSession, a: WorkItem, b: WorkItem, actor: Actor
) -> WorkItemLink:
    """Connect two tickets that concern the same underlying problem.

    Not a merge. Each keeps its own owner, status and conversation - the link
    only says the other one exists, so that whoever is reading either can find
    it.

    Closed tickets are deliberately allowed on both sides. "This is the same
    thing we closed for them last month" is one of the more useful links there
    is, and refusing it would mean the connection can only be recorded while
    both are still live, which is rarely when anyone notices.
    """
    actor.require_any()

    if a.id == b.id:
        raise DomainError("A ticket cannot be linked to itself.")

    already = await existing_link(session, a, b)
    if already is not None:
        raise DomainError(
            f"{a.display_reference} and {b.display_reference} are already linked."
        )

    lower, higher = _pair(a, b)
    link = WorkItemLink(
        lower_work_item_id=lower,
        higher_work_item_id=higher,
        created_by_staff_id=actor.staff.id if actor.staff else None,
        created_by_name=actor.name,
    )
    session.add(link)
    await session.flush()

    # Recorded against both, because the history of either has to show it. A
    # link visible from only one side is not a link, it is a footnote.
    for this, other in ((a, b), (b, a)):
        await record_event(
            session, this, EventType.TICKETS_LINKED, actor,
            other_work_item_id=other.id,
            other_reference=other.display_reference,
            other_subject=other.subject,
        )
    return link


async def unlink_tickets(
    session: AsyncSession, a: WorkItem, b: WorkItem, actor: Actor
) -> bool:
    """Remove a link. Returns False if there was not one.

    The link row goes; the events that recorded it stay, as every event does.
    So removing a link never removes the fact that it was once made, which is
    what makes it safe to let anyone undo their own mistake.
    """
    actor.require_any()

    link = await existing_link(session, a, b)
    if link is None:
        return False

    await session.delete(link)
    await session.flush()

    for this, other in ((a, b), (b, a)):
        await record_event(
            session, this, EventType.TICKETS_UNLINKED, actor,
            other_work_item_id=other.id,
            other_reference=other.display_reference,
        )
    return True


# How far back a client's own list reaches. NexterPay chose four weeks: long
# enough to cover "what happened to the thing from a fortnight ago", short
# enough that a group running for a year does not answer with a wall.
CLIENT_HISTORY = timedelta(weeks=4)


async def open_items_for_chat(
    session: AsyncSession, chat: Chat, *, include_recent_closed: bool = False
) -> list[WorkItem]:
    """Work items raised from a client group, most recently touched first.

    Open ones always. Closed ones only when asked for, and only if they were
    closed inside CLIENT_HISTORY - a client group that has been running a year
    would otherwise return everything it has ever raised, which is not a list
    anybody reads.

    The window is measured from when the request was closed rather than when
    it was raised. A long-running request closed yesterday is recent news; one
    raised yesterday and closed the same day drops out on the same schedule as
    everything else.
    """
    if include_recent_closed:
        since = utcnow() - CLIENT_HISTORY
        condition = (WorkItem.status != WorkItemStatus.CLOSED) | (
            WorkItem.closed_at.is_not(None) & (WorkItem.closed_at >= since)
        )
    else:
        condition = WorkItem.status != WorkItemStatus.CLOSED

    result = await session.execute(
        select(WorkItem)
        .where(WorkItem.source_chat_id == chat.id, condition)
        .order_by(WorkItem.updated_at.desc())
    )
    return list(result.scalars().all())
