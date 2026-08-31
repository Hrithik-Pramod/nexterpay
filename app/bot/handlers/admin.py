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

from app.bot import commands as cmd
from app.bot.registry import (
    deactivate_staff,
    register_client_chat,
    register_operations_chat,
    resolve_chat,
    resolve_staff,
    upsert_staff,
)
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Client, Staff, WorkItem
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


@router.message(Command(cmd.REGISTER_OPS))
async def cmd_register_ops(message: Message, command: CommandObject) -> None:
    """`/np_register_ops <department>` - run inside the Operations Group itself."""
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return
        department = _department(command.args or "")
        if department is None:
            await message.reply(
                f"Usage: /{cmd.REGISTER_OPS} <{Department.usage()}>"
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
        f"Registered this group as {department.label} Operations.\n"
        f"Make sure topics are enabled and the bot can manage them."
    )


@router.message(Command(cmd.REGISTER_CLIENT))
async def cmd_register_client(message: Message, command: CommandObject) -> None:
    """`/np_register_client <department> <client name>` - run in the client group."""
    await _register_counterparty(message, command, is_supplier=False)


async def _register_counterparty(
    message: Message, command: CommandObject, *, is_supplier: bool
) -> None:
    """Shared by the client and supplier commands - they differ by one flag."""
    which = cmd.REGISTER_SUPPLIER if is_supplier else cmd.REGISTER_CLIENT
    noun = "supplier" if is_supplier else "client"
    parts = (command.args or "").split(maxsplit=1)

    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return
        if len(parts) < 2 or _department(parts[0]) is None:
            await message.reply(
                f"Usage: /{which} "
                f"<{Department.usage()}> <{noun} name>"
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
            is_supplier=is_supplier,
        )
        logger.info(
            "Registered %s group %s: %s / %s",
            noun, message.chat.id, client_name, department.value,
        )

    await message.reply(
        f"Registered: {client_name} — {department.label} ({noun}).\n"
        f"Set their four-letter code with /{cmd.SETCODE} <CODE>."
    )


@router.message(Command(cmd.ADDUSER))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    """`/np_adduser <role> <department>` - as a reply to the person being added."""
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return

        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None:
            await message.reply(
                f"Reply to the person you want to add, then send /{cmd.ADDUSER}."
            )
            return

        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.reply(
                f"Usage (as a reply): /{cmd.ADDUSER} "
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


@router.message(Command(cmd.REMOVEUSER))
async def cmd_removeuser(message: Message, command: CommandObject) -> None:
    """`/np_removeuser` - as a reply, or with a telegram id.

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
            await message.reply(
                f"Reply to the person, or use /{cmd.REMOVEUSER} <telegram id>."
            )
            return

        staff = await deactivate_staff(session, target_id)
        if staff is None:
            await message.reply("That user is not registered.")
            return
        logger.info("Deactivated staff %s", target_id)
        name = staff.display_name

    await message.reply(f"{name} deactivated. They can no longer act on work items.")


@router.message(Command(cmd.REGISTER_SUPPLIER))
async def cmd_register_supplier(message: Message, command: CommandObject) -> None:
    """`/np_register_supplier <department> <name>` - run in the supplier's group.

    Identical to registering a client, except the group is marked as a
    supplier so broadcasts can target one or the other. Everything else about
    it behaves the same, because NexterPay confirmed a supplier request is the
    same process with different labels.
    """
    await _register_counterparty(message, command, is_supplier=True)


@router.message(Command(cmd.ADDPARTY))
async def cmd_addparty(message: Message, command: CommandObject) -> None:
    """`/np_addparty <CODE> <name>` - register a counterparty with no group.

    Filing needs the supplier to exist on the platform, but plenty of
    suppliers have no Telegram group with NexterPay and never will. Without
    this they could not be filed against at all, which would make the filing
    structure useless for exactly the cases it was asked for.

    Run in an Operations Group. Creates the counterparty and nothing else -
    no chat, no messages. If a group is set up for them later,
    /np_register_client links it to the same record by name.
    """
    parts = (command.args or "").split(maxsplit=1)

    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return

        if len(parts) < 2:
            await message.reply(
                f"Usage: /{cmd.ADDPARTY} <CODE> <name>\n"
                f"For example: /{cmd.ADDPARTY} SPEX Supplier Pexi"
            )
            return

        code, name = parts[0].strip().upper(), parts[1].strip()
        if not (len(code) == 4 and code.isascii() and code.isalpha()):
            await message.reply("The code must be exactly four letters, for example SPEX.")
            return

        clash = await session.execute(select(Client).where(Client.code == code))
        holder = clash.scalar_one_or_none()
        if holder is not None:
            await message.reply(f"{code} already belongs to {holder.name}.")
            return

        existing = await session.execute(select(Client).where(Client.name == name))
        party = existing.scalar_one_or_none()
        if party is not None:
            was = party.code
            party.code = code
            await session.flush()
            outcome = (
                f"{name} already existed and is now {code}."
                if was is None else f"{name} is now {code} (was {was})."
            )
        else:
            session.add(Client(name=name, code=code))
            await session.flush()
            outcome = f"{name} added as {code}. It can now be filed against."
        logger.info("Counterparty %s registered as %s", name, code)

    await message.reply(outcome)


@router.message(Command(cmd.SETCODE))
async def cmd_setcode(message: Message, command: CommandObject) -> None:
    """`/np_setcode <CODE>` - assign a counterparty's four-letter code.

    Run inside the counterparty's own group, which is how the bot knows who
    the code is for. Four letters, unique across the platform, because the
    code is what every reference and every topic title is built on.
    """
    code = (command.args or "").strip().upper()

    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id if message.from_user else None):
            return

        if not (len(code) == 4 and code.isascii() and code.isalpha()):
            await message.reply(
                f"Usage: /{cmd.SETCODE} <CODE> - exactly four letters, for example ACME."
            )
            return

        chat = await resolve_chat(session, message.chat.id)
        if chat is None or chat.client_id is None:
            await message.reply(
                "This group is not registered to a client or supplier yet. "
                f"Register it first with /{cmd.REGISTER_CLIENT}."
            )
            return

        taken = await session.execute(
            select(Client).where(Client.code == code, Client.id != chat.client_id)
        )
        holder = taken.scalar_one_or_none()
        if holder is not None:
            # Codes lead every reference, so two counterparties sharing one
            # would make references ambiguous - the opposite of filing.
            await message.reply(
                f"{code} already belongs to {holder.name}. Codes must be unique, "
                f"so please choose another."
            )
            return

        counterparty = await session.get(Client, chat.client_id)
        previous = counterparty.code
        counterparty.code = code
        await session.flush()
        logger.info("Set code %s for client %s", code, counterparty.name)

    if previous and previous != code:
        await message.reply(
            f"{counterparty.name} is now {code} (was {previous}). "
            f"Requests already raised keep their original reference."
        )
    else:
        await message.reply(f"{counterparty.name} is now {code}.")


@router.message(Command(cmd.WORKLOAD))
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

    lines = [f"Open work items — {chat.department.label} ({len(items)})", ""]
    for item in items:
        owner = owners.get(item.owner_staff_id, "unassigned")
        lines.append(
            f"{item.display_reference}  [{item.priority.label}]  "
            f"{item.status.label}  — {owner}\n    {item.subject[:60]}"
        )
    await message.reply("\n".join(lines)[:4000])
