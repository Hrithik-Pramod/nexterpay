"""Administration.

NexterPay chose a bot-only build, so there is no console - administration is
these commands. They are intentionally blunt and intentionally logged.

Bootstrapping: the first administrator cannot be added by an administrator, so
`ADMIN_BOOTSTRAP_ID` in the environment names one Telegram user who is treated
as an administrator until a real one exists in the database.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import commands as cmd
from app.bot import keyboards as kb
from app.bot.deps import prompt_for
from app.bot.registry import (
    deactivate_staff,
    leads_for,
    register_client_chat,
    register_operations_chat,
    remove_group_lead,
    remove_staff_from_department,
    resolve_chat,
    resolve_staff,
    set_group_lead,
    upsert_staff,
)
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import Client, Staff, StaffDepartment, WorkItem
from app.domain.enums import ChatKind, Department, StaffRole, WorkItemStatus

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def _is_admin(session, user_id: int | None) -> bool:
    if user_id is None:
        return False
    settings = get_settings()
    if settings.admin_bootstrap_id and user_id == settings.admin_bootstrap_id:
        count = await session.scalar(
            select(func.count())
            .select_from(StaffDepartment)
            .join(Staff, Staff.id == StaffDepartment.staff_id)
            .where(
                StaffDepartment.role == StaffRole.ADMINISTRATOR,
                Staff.is_active.is_(True),
            )
        )
        if not count:
            return True
    staff = await resolve_staff(session, user_id)
    return staff is not None and staff.is_administrator


async def _admin_or_refuse(session, message: Message) -> bool:
    """True if they may run this. Otherwise says so, and returns False.

    Every administrator command in this file used to `return` here without a
    word. The reasoning at the time was that a non-administrator should not
    learn what they cannot do - which is worth almost nothing, since /nphelp
    names the administrator commands to everybody anyway, and costs a great
    deal.

    What it cost: on 4 September NexterPay reported /npsetlead as "not
    working". It was working exactly as designed. Gavin is an Operator, the
    command needs an administrator, and the bot answered him with silence.
    There is no way to tell that apart from a crash, a deploy that failed, a
    typo in the command name, or Telegram not delivering the message - and
    every one of those has a completely different fix. A refusal that does not
    speak turns a permissions question into a bug report.
    """
    user = message.from_user
    if await _is_admin(session, user.id if user else None):
        return True

    person = await resolve_staff(session, user.id) if user else None
    if person is None:
        await message.reply(
            "That one is for administrators, and you are not registered as "
            "NexterPay staff at all.\n\n"
            "An administrator can add you by replying to one of your messages "
            f"with /{cmd.ADDUSER} operator <department>."
        )
    else:
        desks = ", ".join(
            f"{m.department.label} ({m.role.value.replace('_', ' ')})"
            for m in person.desks
        )
        await message.reply(
            "That one is for administrators.\n\n"
            f"You are {desks or 'not on any department'}. "
            f"Send /{cmd.HELP} for what you can do here, or ask an "
            f"administrator to run it."
        )
    return False


def _department(value: str) -> Department | None:
    try:
        return Department(value.strip().lower())
    except ValueError:
        return None


@router.message(cmd.any_case(cmd.REGISTER_OPS))
async def cmd_register_ops(message: Message, command: CommandObject) -> None:
    """`/np_register_ops <department>` - run inside the Operations Group itself."""
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return
        department = _department(command.args or "")
        if department is None:
            await message.reply(
                f"Usage: /{cmd.REGISTER_OPS} <department>\n\n"
                f"Departments: {Department.usage()}.\n"
                f"Send this inside the Operations Group itself, with Topics "
                f"switched on."
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


@router.message(cmd.any_case(cmd.REGISTER_CLIENT))
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
        if not await _admin_or_refuse(session, message):
            return
        if len(parts) < 2 or _department(parts[0]) is None:
            await message.reply(
                f"Usage: /{which} <department> <{noun} name>\n\n"
                f"Departments: {Department.usage()}.\n\n"
                f"Send this inside the {noun}'s own group. If they already "
                f"have a group with us on another desk, use exactly the same "
                f"name - that is what keeps them on one code.\n\n"
                f"Or send /{cmd.SETUP} and pick from buttons instead."
            )
            return
        department = _department(parts[0])
        client_name = parts[1].strip()
        chat = await register_client_chat(
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
        # A counterparty can have a group per department, all sharing one
        # record and therefore one code. Telling someone to set a code that
        # already exists invites them to run /np_setcode here, which would
        # change it for every group that counterparty has - and every
        # reference already quoted in an email.
        existing = await session.get(Client, chat.client_id)
        code = existing.code if existing else None

    if code:
        await message.reply(
            f"Registered: {client_name} — {department.label} ({noun}).\n"
            f"They already have the code {code}, so requests raised here will "
            f"read {code}-1042. Nothing else to do."
        )
        return

    await message.reply(
        f"Registered: {client_name} — {department.label} ({noun}).\n"
        f"Set their four-letter code with /{cmd.SETCODE} <CODE>."
    )


@router.message(cmd.any_case(cmd.ADDUSER))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    """`/np_adduser <role> <department>` - as a reply to the person being added."""
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return

        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None:
            await message.reply(
                f"That has to be a reply. Telegram will not tell a bot who is "
                f"in a group, so pointing at something they wrote is the only "
                f"way it can learn who they are.\n\n"
                f"Reply to a message from them with:\n"
                f"/{cmd.ADDUSER} <role> <department>\n\n"
                f"Roles: {', '.join(r.value for r in StaffRole)}.\n"
                f"Departments: {Department.usage()}.\n\n"
                f"Or send /{cmd.SETUP} and pick from buttons instead."
            )
            return

        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.reply(
                f"Usage, as a reply: /{cmd.ADDUSER} <role> <department>\n\n"
                f"Roles: {', '.join(r.value for r in StaffRole)}.\n"
                f"Departments: {Department.usage()}.\n\n"
                f"Adding somebody to a second department does not move them "
                f"off the first - they keep both, with a role in each."
            )
            return
        try:
            role = StaffRole(parts[0].lower())
        except ValueError:
            await message.reply(
                f"{parts[0]!r} is not a role. One of: "
                f"{', '.join(r.value for r in StaffRole)}."
            )
            return
        department = _department(parts[1])
        if department is None:
            await message.reply(
                f"{parts[1]!r} is not a department. One of: {Department.usage()}."
            )
            return

        staff = await upsert_staff(
            session,
            telegram_user_id=target.id,
            display_name=target.full_name,
            role=role,
            department=department,
        )
        logger.info("Added staff %s (%s) as %s", target.id, target.full_name, role.value)
        name = target.full_name
        # Read inside the session; the relationship is not available after it
        # closes and the reply names every desk on purpose - the old behaviour
        # silently moved people, so saying what they now hold is the point.
        desks = [
            f"{m.department.label} ({m.role.value.replace('_', ' ')})"
            for m in staff.desks
        ]

    await message.reply(
        f"{name} added as {role.value.replace('_', ' ')} in {department.label}.\n"
        f"They now work: {', '.join(desks)}."
    )


@router.message(cmd.any_case(cmd.REMOVEUSER))
async def cmd_removeuser(message: Message, command: CommandObject) -> None:
    """`/np_removeuser [department]` - as a reply, or with a telegram id.

    Naming a department takes that desk off them and leaves the others, which
    is what "they have stopped covering Compliance" means. Naming none removes
    them from the platform entirely.

    Deactivates rather than deletes, so past events keep resolving to a name.
    Offboarding matters more than onboarding here.
    """
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return

        args = (command.args or "").split()
        target_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
        elif args and args[0].isdigit():
            target_id = int(args.pop(0))
        if target_id is None:
            await message.reply(
                f"Reply to the person, or use /{cmd.REMOVEUSER} <telegram id> "
                f"[department]."
            )
            return

        department = _department(args[0]) if args else None
        if args and department is None:
            await message.reply(f"Unknown department. One of: {Department.usage()}.")
            return

        if department is None:
            staff = await deactivate_staff(session, target_id)
            if staff is None:
                await message.reply("That user is not registered.")
                return
            logger.info("Deactivated staff %s", target_id)
            reply = (
                f"{staff.display_name} deactivated. They can no longer act on "
                f"work items."
            )
        else:
            staff, was_last = await remove_staff_from_department(
                session, target_id, department
            )
            if staff is None:
                await message.reply("That user is not registered.")
                return
            remaining = [m.department.label for m in staff.desks]
            logger.info(
                "Removed staff %s from %s (last=%s)", target_id, department.value, was_last
            )
            if was_last:
                reply = (
                    f"{staff.display_name} removed from {department.label}. That was "
                    f"their only department, so they have been deactivated."
                )
            elif not remaining:
                reply = f"{staff.display_name} was not registered for {department.label}."
            else:
                reply = (
                    f"{staff.display_name} removed from {department.label}. "
                    f"They still work: {', '.join(remaining)}."
                )

    await message.reply(reply)


@router.message(cmd.any_case(cmd.REGISTER_SUPPLIER))
async def cmd_register_supplier(message: Message, command: CommandObject) -> None:
    """`/np_register_supplier <department> <name>` - run in the supplier's group.

    Identical to registering a client, except the group is marked as a
    supplier so broadcasts can target one or the other. Everything else about
    it behaves the same, because NexterPay confirmed a supplier request is the
    same process with different labels.
    """
    await _register_counterparty(message, command, is_supplier=True)


@router.message(cmd.any_case(cmd.ADDPARTY))
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
        if not await _admin_or_refuse(session, message):
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


@router.message(cmd.any_case(cmd.SETCODE))
async def cmd_setcode(message: Message, command: CommandObject) -> None:
    """`/np_setcode <CODE>` - assign a counterparty's four-letter code.

    Run inside the counterparty's own group, which is how the bot knows who
    the code is for. Four letters, unique across the platform, because the
    code is what every reference and every topic title is built on.
    """
    code = (command.args or "").strip().upper()

    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
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


@router.message(cmd.any_case(cmd.WORKLOAD))
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
        # Only the outbound ones are marked. Most work is inbound, so labelling
        # both sides here would put the same word on nearly every line and stop
        # anyone noticing it. The pinned header spells both out; a list wants
        # the exception to stand out.
        direction = "  · outbound" if item.raised_by_us else ""
        lines.append(
            f"{item.display_reference}  [{item.priority.label}]  "
            f"{item.status.label}  — {owner}{direction}\n    {item.subject[:60]}"
        )
    await message.reply("\n".join(lines)[:4000])


@router.message(cmd.any_case(cmd.SETLEAD))
async def cmd_setlead(message: Message) -> None:
    """`/npsetlead` - as a reply, inside the counterparty's own group.

    Telegram will not tell a bot who is in a group, so the only way to learn
    somebody's identity is for them to speak and for us to point at it. Same
    mechanism as registering staff, for the same reason.
    """
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return

        chat = await resolve_chat(session, message.chat.id)
        if chat is None or chat.kind is not ChatKind.CLIENT:
            await message.reply(
                "Send this inside the client or supplier group, as a reply to a "
                "message from the person you want to name."
            )
            return

        target = message.reply_to_message.from_user if message.reply_to_message else None
        if target is None:
            await message.reply(
                f"That has to be a reply. Telegram will not tell a bot who is "
                f"in a group, so pointing at something they wrote is the only "
                f"way it can learn who they are.\n\n"
                f"Reply to a message from them with /{cmd.SETLEAD}."
            )
            return

        await set_group_lead(
            session, chat,
            telegram_user_id=target.id, display_name=target.full_name,
        )
        logger.info("Named %s as a lead for chat %s", target.id, message.chat.id)
        names = [lead.display_name for lead in await leads_for(session, chat)]

    await message.reply(
        f"{target.full_name} is now a named contact for this group.\n"
        f"Contacts: {', '.join(names)}."
    )


@router.message(cmd.any_case(cmd.LEADS))
async def cmd_leads(message: Message) -> None:
    """`/npleads` - who is named for this group."""
    async with session_scope() as session:
        chat = await resolve_chat(session, message.chat.id)
        if chat is None:
            return
        names = [lead.display_name for lead in await leads_for(session, chat)]

    if not names:
        await message.reply(
            f"Nobody is named for this group yet. An administrator can add "
            f"someone by replying to one of their messages with /{cmd.SETLEAD}."
        )
        return
    await message.reply("Named contacts here: " + ", ".join(names) + ".")


@router.message(cmd.any_case(cmd.REMOVELEAD))
async def cmd_removelead(message: Message) -> None:
    """`/npremovelead` - as a reply. Deactivated, not deleted."""
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return

        chat = await resolve_chat(session, message.chat.id)
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if chat is None or target is None:
            await message.reply(
                f"Reply to a message from the person, then send /{cmd.REMOVELEAD}."
            )
            return

        removed = await remove_group_lead(session, chat, target.id)
        remaining = [lead.display_name for lead in await leads_for(session, chat)]

    if removed is None:
        await message.reply(f"{target.full_name} was not a named contact here.")
        return
    await message.reply(
        f"{target.full_name} removed.\n"
        + (f"Contacts: {', '.join(remaining)}." if remaining
           else "Nobody is named for this group now.")
    )


# --------------------------------------------------------------------------
# Administration by button
#
# `/npsetup` covers the two jobs NexterPay named: registering a group, and
# adding a person. Both are done with somebody standing there waiting, and
# both are typed from memory - "senior_operator" and the department spelling
# between them account for most of the failed attempts so far.
#
# Everything else stays a command. Buttons for all of it was a much larger
# piece than it sounds, and the rest are run once, calmly, by someone with
# the reference open.
# --------------------------------------------------------------------------

class Setup(StatesGroup):
    awaiting_name = State()


@router.message(cmd.any_case(cmd.SETUP))
async def cmd_setup(message: Message, state: FSMContext) -> None:
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return
        chat = await resolve_chat(session, message.chat.id)
        in_operations = chat is not None and chat.kind is ChatKind.OPERATIONS

    await state.clear()
    await message.reply(
        "What would you like to set up here?"
        if chat is not None
        else "This group is not registered yet. What is it?",
        reply_markup=kb.setup_menu(
            in_operations=in_operations, registered=chat is not None
        ),
    )


@router.callback_query(F.data.startswith(f"{kb.ADMIN_PREFIX}:"))
async def on_setup(query: CallbackQuery, state: FSMContext) -> None:
    try:
        action, value = kb.parse_admin_cb(query.data or "")
    except ValueError:
        await query.answer()
        return

    async with session_scope() as session:
        if not await _is_admin(session, query.from_user.id if query.from_user else None):
            await query.answer("Administrators only.", show_alert=True)
            return

    if action == "cancel":
        await state.clear()
        await query.message.edit_text("Cancelled. Nothing was changed.")
        await query.answer()
        return

    if action == "regops":
        await query.message.edit_text(
            "Which desk is this Operations Group for?",
            reply_markup=kb.department_menu("opsdept"),
        )
        await query.answer()
        return

    if action == "opsdept":
        # Registered here and now. Unlike a counterparty group there is no
        # name to ask for - an Operations Group is ours, and the department
        # is the whole of what distinguishes one from another.
        department = _department(value)
        if department is None:
            await query.answer("That department no longer exists.", show_alert=True)
            return
        async with session_scope() as session:
            await register_operations_chat(
                session,
                telegram_chat_id=query.message.chat.id,
                department=department,
                title=query.message.chat.title,
            )
        logger.info(
            "Registered ops group %s as %s by button",
            query.message.chat.id, department.value,
        )
        await query.message.edit_text(
            f"Registered this group as {department.label} Operations.\n\n"
            f"Two things to check before using it: Topics must be switched on "
            f"in the group settings, and the bot needs to be an administrator "
            f"here with Manage Topics. Without both, requests have nowhere to "
            f"land."
        )
        await query.answer()
        return

    if action == "howlead":
        await query.message.edit_text(
            f"Naming a contact has to be done as a reply, so reply to a "
            f"message from the person with:\n\n"
            f"/{cmd.SETLEAD}\n\n"
            f"Telegram will not tell a bot who is in a group, so pointing at "
            f"something they wrote is the only way it can learn who they are. "
            f"Once named, replying to this group offers to tag them, so the "
            f"message reaches a person rather than a room.\n\n"
            f"/{cmd.LEADS} shows who is named here."
        )
        await query.answer()
        return

    if action in ("regclient", "regsupplier"):
        await state.update_data(is_supplier=action == "regsupplier")
        await query.message.edit_text(
            "Which department is this group for?",
            reply_markup=kb.department_menu("regdept"),
        )
        await query.answer()
        return

    if action == "regdept":
        await state.update_data(department=value)
        await state.set_state(Setup.awaiting_name)
        text, markup, mode = prompt_for(
            query.from_user,
            "What is this counterparty called? Use the name exactly as it is "
            "already registered, if they have another group with us - that is "
            "what keeps them on one code.",
            placeholder="Counterparty name",
        )
        await query.message.answer(text, reply_markup=markup, parse_mode=mode)
        await query.answer()
        return

    if action == "adduser":
        # No attempt to read a person off this message. The reply is on the
        # /npsetup message, not on them - which is why this flow ends by
        # handing back a command to send as a reply rather than doing it here.
        await query.message.edit_text(
            "Which department are they joining?",
            reply_markup=kb.department_menu("userdept"),
        )
        await query.answer()
        return

    if action == "userdept":
        from app.domain.enums import Department as Dept

        await query.message.edit_text(
            f"What can they do in {Dept(value).label}?",
            reply_markup=kb.role_menu(Dept(value)),
        )
        await query.answer()
        return

    if action == "setrole":
        department_value, role_value = value.split("|")
        await query.message.edit_text(
            f"Reply to a message from the person with:\n\n"
            f"/{cmd.ADDUSER} {role_value} {department_value}\n\n"
            f"The bot has no way to learn who somebody is except by being "
            f"pointed at something they wrote - Telegram will not list the "
            f"members of a group."
        )
        await query.answer()
        return

    await query.answer()


@router.message(Setup.awaiting_name)
async def capture_setup_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.reply("Type the name, or ignore this to abandon it.")
        return

    data = await state.get_data()
    department = _department(data.get("department", ""))
    if department is None:
        await state.clear()
        await message.reply("That setup expired. Start again with /npsetup.")
        return

    await state.clear()
    async with session_scope() as session:
        if not await _admin_or_refuse(session, message):
            return
        chat = await register_client_chat(
            session,
            telegram_chat_id=message.chat.id,
            client_name=name,
            department=department,
            title=message.chat.title,
            is_supplier=bool(data.get("is_supplier")),
        )
        existing = await session.get(Client, chat.client_id)
        code = existing.code if existing else None
        noun = "supplier" if data.get("is_supplier") else "client"

    if code:
        await message.reply(
            f"Registered: {name} — {department.label} ({noun}).\n"
            f"They already have the code {code}. Nothing else to do."
        )
        return
    await message.reply(
        f"Registered: {name} — {department.label} ({noun}).\n"
        f"Set their four-letter code with /{cmd.SETCODE} <CODE>."
    )
