"""Chat and staff resolution.

The bot never acts in a group it does not recognise. Every update is resolved
against the registry first; unknown chats are dropped silently rather than
answered, so the bot is inert anywhere it has not been deliberately registered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, Client, Staff
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
        staff = Staff(telegram_user_id=telegram_user_id, display_name=display_name,
                      role=role, department=department)
        session.add(staff)
    else:
        staff.display_name = display_name
        staff.role = role
        staff.department = department
        staff.is_active = True
        staff.deactivated_at = None
    await session.flush()
    return staff


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
