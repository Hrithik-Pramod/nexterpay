"""Shared handler plumbing.

Handlers stay thin: resolve the chat, resolve the actor, call a service,
translate a domain error into a reply. Anything more belongs in `app/services`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.registry import resolve_chat, resolve_staff
from app.db.models import Chat, WorkItem
from app.domain.enums import ChatKind
from app.domain.errors import DomainError
from app.domain.work_items import Actor

logger = logging.getLogger(__name__)

_gateway = None


def set_gateway(gateway) -> None:
    global _gateway
    _gateway = gateway


def gateway():
    if _gateway is None:
        raise RuntimeError("Gateway not configured - call set_gateway() at startup")
    return _gateway


@dataclass
class Context:
    chat: Chat
    actor: Actor | None


async def client_context(session: AsyncSession, telegram_chat_id: int) -> Chat | None:
    """A registered client group, or None. The bot is inert elsewhere."""
    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None or chat.kind is not ChatKind.CLIENT:
        return None
    return chat


# Telegram's stand-in user for a group admin posting anonymously. When
# "Remain Anonymous" is on, messages arrive from the group rather than the
# person, so there is no identity to match against a staff record.
ANONYMOUS_ADMIN_ID = 1087968824


def is_anonymous_admin(telegram_user_id: int | None) -> bool:
    return telegram_user_id == ANONYMOUS_ADMIN_ID


async def staff_context(
    session: AsyncSession, telegram_chat_id: int, telegram_user_id: int | None
) -> tuple[Chat, Actor] | None:
    """A registered Operations Group plus an active staff member, or None."""
    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None or chat.kind is not ChatKind.OPERATIONS or telegram_user_id is None:
        return None
    if is_anonymous_admin(telegram_user_id):
        return None
    staff = await resolve_staff(session, telegram_user_id)
    if staff is None:
        return None
    if staff.department is not chat.department and staff.role.value != "administrator":
        # Staff work their own department's group. Administrators are exempt so
        # they can configure any of them.
        return None
    return chat, Actor.of(staff)


def refusal_reason(telegram_user_id: int | None) -> str:
    """Why an action was refused, in words the person can act on.

    "You are not registered" is unhelpful when the real problem is that
    Telegram is hiding who they are.
    """
    if is_anonymous_admin(telegram_user_id):
        return (
            "You are posting anonymously, so I cannot tell who you are. "
            "Turn off 'Remain Anonymous' in your admin rights for this group, "
            "or ask to be removed as a Telegram admin - staff only need to be "
            "members here."
        )
    return (
        "You are not registered as active staff for this department. "
        "An administrator can add you: ask them to reply to one of your "
        "messages with /adduser operator <department>."
    )


async def work_item_for_thread(
    session: AsyncSession, chat: Chat, thread_id: int | None
) -> WorkItem | None:
    """Map a topic back to its work item - the reverse of topic creation."""
    if thread_id is None:
        return None
    result = await session.execute(
        select(WorkItem).where(
            WorkItem.operations_chat_id == chat.id,
            WorkItem.topic_id == thread_id,
        )
    )
    return result.scalar_one_or_none()


def explain(exc: Exception) -> str:
    """Domain errors are safe to show staff; anything else is not."""
    if isinstance(exc, DomainError):
        return str(exc)
    logger.exception("Unexpected error in handler")
    return "Something went wrong. The error has been logged."
