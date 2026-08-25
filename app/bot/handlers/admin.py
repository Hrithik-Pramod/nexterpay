"""Administration.

NexterPay chose a bot-only build, so there is no console - administration is
these commands. They are intentionally blunt and intentionally logged.

Bootstrapping: the first administrator cannot be added by an administrator, so
`ADMIN_BOOTSTRAP_ID` in the environment names one Telegram user who is treated
as an administrator until a real one exists in the database.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot.registry import (
    deactivate_staff,
    register_client_chat,
    register_operations_chat,
    resolve_staff,
    upsert_staff,
)
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Staff, WorkItem
from app.domain.enums import Department, StaffRole, WorkItemStatus

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def _is_admin(session, user_id: int | None) -> bool:
    if user_id is None:
        return False
    settings = get_settings()
    if settings.admin_bootstrap_id and user_id == settings.admin_bootstrap_id:
        count = await session.scalar(
            select(func.count()).select_from(Staff).where(
                Staff.role == StaffRole.ADMINISTRATOR, Staff.is_active.is_(True)
            )
        )
        if not count:
            return True
    staff = await resolve_staff(session, user_id)
    return staff is not None and staff.role is StaffRole.ADMINISTRATOR


def _department(value: str) -> Department | None:
    try:
        return Department(value.strip().lower())
    except ValueError:
        return None


@router.message(Command("register_ops"))
async def cmd_register_ops(message: Message, command: CommandObject) -> None:
    """/register_ops <department> — run inside the Operations Group itself."""
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return
        department = _department(command.args or "")
        if department is None:
            await message.reply(
                "Usage: /register_ops <support|finance|development|business>"
            )
            return
        await register_operations_chat(
            session,
            telegram_chat_id=message.chat.id,
            department=department,
            title=message.chat.title,
        )
        logger.info("Registered ops group %s as %s", message.chat.id, department.value)

    await message.reply(
        f"Registered this group as {department.value.title()} Operations.\n"
        f"Make sure topics are enabled and the bot can manage them."
    )


@router.message(Command("register_client"))
async def cmd_register_client(message: Message, command: CommandObject) -> None:
    """/register_client <department> <client name> — run inside the client group."""
    parts = (command.args or "").split(maxsplit=1)
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return
        if len(parts) < 2 or _department(parts[0]) is None:
            await message.reply(
                "Usage: /register_client <support|finance|development|business> <client name>"
            )
            return
        department = _department(parts[0])
        client_name = parts[1].strip()
        await register_client_chat(
            session,
            telegram_chat_id=message.chat.id,
            client_name=client_name,
            department=department,
            title=message.chat.title,
        )
        logger.info(
            "Registered client group %s: %s / %s", message.chat.id, client_name, department.value
        )

    await message.reply(f"Registered: {client_name} — {department.value.title()}.")


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    """/adduser <role> <department> — as a reply to the person being added."""
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return

        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None:
            await message.reply("Reply to the person you want to add, then send /adduser.")
            return

        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.reply(
                "Usage (as a reply): /adduser "
                "<operator|senior_operator|manager|administrator> <department>"
            )
            return
        try:
            role = StaffRole(parts[0].lower())
        except ValueError:
            await message.reply("Unknown role.")
            return
        department = _department(parts[1])
        if department is None:
            await message.reply("Unknown department.")
            return

        await upsert_staff(
            session,
            telegram_user_id=target.id,
            display_name=target.full_name,
            role=role,
            department=department,
        )
        logger.info("Added staff %s (%s) as %s", target.id, target.full_name, role.value)
        name = target.full_name

    await message.reply(f"{name} added as {role.value} in {department.value}.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject) -> None:
    """/removeuser — as a reply, or /removeuser <telegram id>.

    Deactivates rather than deletes, so past events keep resolving to a name.
    Offboarding matters more than onboarding here.
    """
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return

        target_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
        elif command.args and command.args.strip().isdigit():
            target_id = int(command.args.strip())
        if target_id is None:
            await message.reply("Reply to the person, or use /removeuser <telegram id>.")
            return

        staff = await deactivate_staff(session, target_id)
        if staff is None:
            await message.reply("That user is not registered.")
            return
        logger.info("Deactivated staff %s", target_id)
        name = staff.display_name

    await message.reply(f"{name} deactivated. They can no longer act on work items.")


@router.message(Command("workload"))
async def cmd_workload(message: Message) -> None:
    """Open items by owner for this department.

    The nearest thing to a management view in a bot-only build. If NexterPay
    later want this on a screen, that is Phase 2.
    """
    from app.bot.deps import staff_context

    async with session_scope() as session:
        ctx = await staff_context(
            session, message.chat.id, message.from_user.id if message.from_user else None
        )
        if ctx is None:
            return
        chat, _ = ctx

        result = await session.execute(
            select(WorkItem).where(
                WorkItem.operations_chat_id == chat.id,
                WorkItem.status != WorkItemStatus.CLOSED,
            ).order_by(WorkItem.priority, WorkItem.created_at)
        )
        items = list(result.scalars().all())

        owners: dict[int, str] = {}
        for staff in (await session.execute(select(Staff))).scalars().all():
            owners[staff.id] = staff.display_name

    if not items:
        await message.reply("No open work items.")
        return

    lines = [f"Open work items — {chat.department.value.title()} ({len(items)})", ""]
    for item in items:
        owner = owners.get(item.owner_staff_id, "unassigned")
        lines.append(
            f"{item.display_reference}  [{item.priority.label}]  "
            f"{item.status.label}  — {owner}\n    {item.subject[:60]}"
        )
    await message.reply("\n".join(lines)[:4000])
