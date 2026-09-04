"""Shared handler plumbing.

Handlers stay thin: resolve the chat, resolve the actor, call a service,
translate a domain error into a reply. Anything more belongs in `app/services`.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import commands
from app.bot.registry import resolve_chat, resolve_staff
from app.db.models import Chat, WorkItem
from app.domain.enums import ChatKind, StaffRole
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

    # The role for *this* desk, not the person's most senior one anywhere.
    # Someone who is a Manager in Support and an Operator in Compliance must
    # not be able to reopen a Compliance ticket.
    role = staff.role_in(chat.department)
    if role is None:
        if not staff.is_administrator:
            return None
        # Administrators configure every department, so they are admitted to
        # a group they do not belong to - with administrator rights, which is
        # what they already had everywhere else.
        return chat, Actor(
            name=staff.display_name,
            staff=staff,
            telegram_user_id=staff.telegram_user_id,
            role=StaffRole.ADMINISTRATOR,
        )
    return chat, Actor.of(staff, chat.department)


async def refusal_reason(
    telegram_user_id: int | None, session: AsyncSession | None = None,
    telegram_chat_id: int | None = None,
) -> str:
    """Why an action was refused, in words the person can act on.

    staff_context returns None for three quite different reasons and the
    caller cannot tell them apart, so this works out which it was. Getting
    this wrong is not cosmetic: "you are not registered as staff" sent
    someone hunting for a permissions problem when they were simply standing
    in the wrong room.
    """
    if is_anonymous_admin(telegram_user_id):
        return (
            "You are posting anonymously, so I cannot tell who you are. "
            "Turn off 'Remain Anonymous' in your admin rights for this group, "
            "or ask to be removed as a Telegram admin - staff only need to be "
            "members here."
        )

    if session is not None and telegram_chat_id is not None:
        chat = await resolve_chat(session, telegram_chat_id)
        if chat is None:
            return (
                "This group is not registered. An administrator needs to "
                "register it before the bot will do anything here."
            )
        if chat.kind is not ChatKind.OPERATIONS:
            return (
                "This is a client group, and that is a staff command. Send it "
                "in your Operations Group instead - the internal one where the "
                "request topics are."
            )

        if telegram_user_id is not None:
            staff = await resolve_staff(session, telegram_user_id)
            if staff is not None and staff.role_in(chat.department) is None:
                desks = ", ".join(d.label for d in staff.departments) or "no departments"
                return (
                    f"You are registered for {desks}, and this is the "
                    f"{chat.department.label} group. You can belong to more than "
                    f"one - ask an administrator to add you here as well, with "
                    f"/{commands.ADDUSER} <role> {chat.department.value}."
                )

    return (
        "You are not registered as active staff for this department. "
        "An administrator can add you: ask them to reply to one of your "
        f"messages with /{commands.ADDUSER} operator <department>."
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


def prompt_for(user, text: str, placeholder: str | None = None) -> tuple[str, object, str]:
    """A prompt that actually opens the reply box for the person who asked.

    Returns (text, reply_markup, parse_mode) ready to pass to `answer`.

    This exists because the obvious way to write it is wrong, and was wrong in
    four places. `ForceReply(selective=True)` does not mean "force a reply from
    whoever triggered this" - it means "force a reply from the users mentioned
    in this message", and a plain name in the text is not a mention, it is just
    letters. Written that way the composer opens for nobody: the prompt appears,
    the person waits, nothing happens, and they report the feature as broken.

    A `tg://user` link is a real text_mention entity, which is what `selective`
    looks for. So the name has to be a link, or `selective` has to be False.

    It was fixed in the client Raise Request flow in August and left wrong in
    broadcasting, outbound raising, and the staff reply and note prompts -
    which is how NexterPay came to report that broadcasting "did not work" in
    two different groups.
    """
    from aiogram.types import ForceReply

    if user is None:
        return text, ForceReply(selective=False), None

    who = html.escape(user.full_name)
    body = f'<a href="tg://user?id={user.id}">{who}</a>, {text[0].lower()}{text[1:]}'
    return body, ForceReply(selective=True, input_field_placeholder=placeholder), "HTML"
