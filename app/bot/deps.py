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


async def staff_context(
    session: AsyncSession, telegram_chat_id: int, telegram_user_id: int | None
) -> tuple[Chat, Actor] | None:
    """A registered Operations Group plus an active staff member, or None."""
    chat = await resolve_chat(session, telegram_chat_id)
    if chat is None or chat.kind is not ChatKind.OPERATIONS or telegram_user_id is None:
        return None
    staff = await resolve_staff(session, telegram_user_id)
    if staff is None:
        return None
    if staff.department is not chat.department and staff.role.value != "administrator":
        # Staff work their own department's group. Administrators are exempt so
        # they can configure any of them.
        return None
    return chat, Actor.of(staff)


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
