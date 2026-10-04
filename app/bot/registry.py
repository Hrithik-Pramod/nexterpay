"""Chat and staff resolution.

The bot never acts in a group it does not recognise. Every update is resolved
against the registry first; unknown chats are dropped silently rather than
answered, so the bot is inert anywhere it has not been deliberately registered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, Client, GroupLead, Setting, Staff, StaffDepartment
from app.domain.enums import ChatKind, Department, StaffRole


async def resolve_chat(session: AsyncSession, telegram_chat_id: int) -> Chat | None:
    result = await session.execute(
        select(Chat).where(
            Chat.telegram_chat_id == telegram_chat_id,
            Chat.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def resolve_staff(session: AsyncSession, telegram_user_id: int) -> Staff | None:
    """Active staff only. A deactivated account resolves to None and is
    therefore refused every action, which is the point of soft deletion."""
    result = await session.execute(
        select(Staff).where(
            Staff.telegram_user_id == telegram_user_id,
            Staff.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def register_client_chat(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    client_name: str,
    department: Department,
    title: str | None = None,
    is_supplier: bool = False,
) -> Chat:
    result = await session.execute(select(Client).where(Client.name == client_name))
    client = result.scalar_one_or_none()
    if client is None:
        client = Client(name=client_name)
        session.add(client)
        await session.flush()

    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None:
        chat = Chat(telegram_chat_id=telegram_chat_id, kind=ChatKind.CLIENT)
        session.add(chat)

    chat.kind = ChatKind.CLIENT
    chat.client_id = client.id
    chat.department = department
    chat.title = title
    chat.is_supplier = is_supplier
    chat.is_active = True
    await session.flush()
    return chat


async def register_operations_chat(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    department: Department,
    title: str | None = None,
) -> Chat:
    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None:
        chat = Chat(telegram_chat_id=telegram_chat_id, kind=ChatKind.OPERATIONS)
        session.add(chat)

    chat.kind = ChatKind.OPERATIONS
    chat.client_id = None
    chat.department = department
    chat.title = title
    # An Operations Group belongs to NexterPay, so it is never a counterparty
    # of either sort - and must never be a broadcast recipient.
    chat.is_supplier = False
    chat.is_active = True
    await session.flush()
    return chat


async def register_archive_chat(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    department: Department,
    title: str | None = None,
) -> Chat:
    """The group closed work is moved into, one per desk.

    Registered exactly like an Operations Group and deliberately so - it is
    another NexterPay-owned forum, not a counterparty room, and must never be
    a broadcast recipient either.

    Nothing is ever raised here and nobody is assigned here. It exists so the
    live topic list stays short, which is the whole of NexterPay's reasoning
    on 9 September.
    """
    # One per desk, checked here rather than by a unique index. A partial index
    # on a newly added enum value cannot be created in the migration that adds
    # the value, and the check is more useful here anyway: it can name the
    # group that already holds the job.
    existing = await session.execute(
        select(Chat).where(
            Chat.kind == ChatKind.ARCHIVE,
            Chat.department == department,
            Chat.is_active.is_(True),
            Chat.telegram_chat_id != telegram_chat_id,
        )
    )
    clash = existing.scalar_one_or_none()
    if clash is not None:
        raise ValueError(
            f"{department.label} already archives to "
            f"“{clash.title or clash.telegram_chat_id}”. A desk can only have "
            f"one archive, or its history ends up split across two groups with "
            f"nothing to make the mistake visible."
        )

    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None:
        chat = Chat(telegram_chat_id=telegram_chat_id, kind=ChatKind.ARCHIVE)
        session.add(chat)

    chat.kind = ChatKind.ARCHIVE
    chat.client_id = None
    chat.department = department
    chat.title = title
    chat.is_supplier = False
    chat.is_active = True
    await session.flush()
    return chat


async def upsert_staff(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    display_name: str,
    role: StaffRole,
    department: Department,
) -> Staff:
    result = await session.execute(
        select(Staff).where(Staff.telegram_user_id == telegram_user_id)
    )
    staff = result.scalar_one_or_none()
    if staff is None:
        # memberships=[] is not decoration. Without it the collection is
        # unloaded on a freshly flushed object, and the next line touching it
        # triggers a lazy load inside async code - MissingGreenlet, again.
        staff = Staff(
            telegram_user_id=telegram_user_id, display_name=display_name, memberships=[]
        )
        session.add(staff)
        await session.flush()
    else:
        staff.display_name = display_name
        staff.is_active = True
        staff.deactivated_at = None

    # Adds a desk; it does not move them off the others. This used to
    # overwrite, so registering someone for Compliance quietly removed them
    # from Support and they discovered it by being refused their own work.
    existing = next(
        (m for m in staff.memberships if m.department is department), None
    )
    if existing is None:
        staff.memberships.append(StaffDepartment(department=department, role=role))
    else:
        existing.role = role

    await session.flush()
    await session.refresh(staff, ["memberships"])
    return staff


async def remove_staff_from_department(
    session: AsyncSession, telegram_user_id: int, department: Department
) -> tuple[Staff | None, bool]:
    """Take one desk off someone, leaving the rest.

    Returns the person and whether that was their last department. Losing the
    last one deactivates them, because a registered person who works nowhere
    would otherwise resolve as staff and be refused every action with a
    message about seniority rather than about not being there at all.
    """
    from app.db.base import utcnow

    result = await session.execute(
        select(Staff).where(Staff.telegram_user_id == telegram_user_id)
    )
    staff = result.scalar_one_or_none()
    if staff is None:
        return None, False

    membership = next(
        (m for m in staff.memberships if m.department is department), None
    )
    if membership is None:
        return staff, False

    staff.memberships.remove(membership)
    await session.flush()
    await session.refresh(staff, ["memberships"])

    if not staff.memberships:
        staff.is_active = False
        staff.deactivated_at = utcnow()
        await session.flush()
        return staff, True
    return staff, False


async def deactivate_staff(session: AsyncSession, telegram_user_id: int) -> Staff | None:
    """Offboarding. Preserved rather than deleted so historical events keep
    resolving to a name."""
    from app.db.base import utcnow

    result = await session.execute(
        select(Staff).where(Staff.telegram_user_id == telegram_user_id)
    )
    staff = result.scalar_one_or_none()
    if staff is None:
        return None
    staff.is_active = False
    staff.deactivated_at = utcnow()
    await session.flush()
    return staff


async def set_group_lead(
    session: AsyncSession, chat: Chat, *, telegram_user_id: int, display_name: str
) -> GroupLead:
    """Name a contact inside a client or supplier group.

    Idempotent, and revives someone previously removed rather than colliding
    with the unique constraint - people come back.
    """
    result = await session.execute(
        select(GroupLead).where(
            GroupLead.chat_id == chat.id,
            GroupLead.telegram_user_id == telegram_user_id,
        )
    )
    lead = result.scalar_one_or_none()
    if lead is None:
        lead = GroupLead(
            chat_id=chat.id,
            telegram_user_id=telegram_user_id,
            display_name=display_name,
        )
        session.add(lead)
    else:
        lead.display_name = display_name
        lead.is_active = True
    await session.flush()
    return lead


async def leads_for(session: AsyncSession, chat: Chat) -> list[GroupLead]:
    result = await session.execute(
        select(GroupLead)
        .where(GroupLead.chat_id == chat.id, GroupLead.is_active.is_(True))
        .order_by(GroupLead.display_name)
    )
    return list(result.scalars().all())


async def preferred_lead_for(
    session: AsyncSession, chat: Chat, *, currency: str | None = None
) -> GroupLead | None:
    """The person this desk always asks in this group.

    Jason, 3 October: "there will be key people in some group he always ask,
    so we can use lead to identify."

    Resolved from the most specific preference to the least: somebody marked
    for this currency, then somebody marked for the group generally, then
    nobody. Narrower wins because that is the only ordering that makes
    setting a narrower one mean anything - a general preference that
    overrode "for XOF ask Marco" would make the second setting pointless.

    Returns None rather than falling back to any lead at all. The question is
    "who do we always ask", and the answer when nobody has said is not a
    name picked off a list.
    """
    result = await session.execute(
        select(GroupLead).where(
            GroupLead.chat_id == chat.id,
            GroupLead.is_active.is_(True),
            GroupLead.is_preferred.is_(True),
        )
    )
    preferred = list(result.scalars().all())
    if not preferred:
        return None

    if currency:
        wanted = currency.strip().upper()
        for lead in preferred:
            if (lead.for_currency or "").upper() == wanted:
                return lead

    for lead in preferred:
        if not lead.for_currency:
            return lead
    return None


async def set_preferred_lead(
    session: AsyncSession,
    chat: Chat,
    telegram_user_id: int,
    *,
    currency: str | None = None,
) -> GroupLead | None:
    """Mark somebody as the one this desk always asks.

    One preference per scope. Setting a new general preference clears the old
    one, and setting one for a currency clears only the old one for that
    currency - otherwise "always ask Marco for XOF" would quietly unseat
    "always ask Amina" for everything else, which is not what anybody meant.
    """
    result = await session.execute(
        select(GroupLead).where(
            GroupLead.chat_id == chat.id,
            GroupLead.telegram_user_id == telegram_user_id,
        )
    )
    lead = result.scalar_one_or_none()
    if lead is None:
        return None

    scope = (currency or "").strip().upper() or None

    existing = await session.execute(
        select(GroupLead).where(
            GroupLead.chat_id == chat.id,
            GroupLead.is_preferred.is_(True),
        )
    )
    for other in existing.scalars().all():
        if (other.for_currency or None) == scope and other.id != lead.id:
            other.is_preferred = False
            other.for_currency = None

    lead.is_preferred = True
    lead.for_currency = scope
    lead.is_active = True
    await session.flush()
    return lead


async def remove_group_lead(
    session: AsyncSession, chat: Chat, telegram_user_id: int
) -> GroupLead | None:
    """Deactivated rather than deleted, so a past mention still resolves."""
    result = await session.execute(
        select(GroupLead).where(
            GroupLead.chat_id == chat.id,
            GroupLead.telegram_user_id == telegram_user_id,
        )
    )
    lead = result.scalar_one_or_none()
    if lead is None:
        return None
    lead.is_active = False
    await session.flush()
    return lead


# --------------------------------------------------------------------------
# Settings somebody can change without a deploy
# --------------------------------------------------------------------------

async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row else None


async def set_setting(
    session: AsyncSession, key: str, value: str | None, *, by: str | None = None
) -> Setting:
    """Upsert, keeping who changed it.

    Who matters more than it looks for the wallet address: a payment arriving
    at the wrong place is discovered days later by a client chasing money,
    and the first question is who changed it and when.
    """
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value, updated_by_name=by)
        session.add(row)
    else:
        row.value = value
        row.updated_by_name = by
    await session.flush()
    return row
