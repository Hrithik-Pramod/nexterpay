"""Check the bot can actually do its job, before anyone starts testing.

Nearly every failed first test is one of five things: topics not enabled, the
bot not an administrator, `can_manage_topics` not granted, the group not
registered, or the wrong chat id. This finds all five in about ten seconds and
tells you which one it is.

    python scripts/preflight.py

Run it after registering groups and before letting anyone loose on the bot.
Exit code is non-zero if anything would block testing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import init_engine, session_scope  # noqa: E402
from app.db.models import Chat, Staff  # noqa: E402
from app.domain.enums import ChatKind, StaffRole  # noqa: E402

OK = "  [ok]   "
WARN = "  [warn] "
FAIL = "  [FAIL] "

problems: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"{OK}{msg}")


def warn(msg: str) -> None:
    print(f"{WARN}{msg}")
    warnings.append(msg)


def fail(msg: str, fix: str) -> None:
    print(f"{FAIL}{msg}\n           fix: {fix}")
    problems.append(msg)


def heading(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


async def check_bot(bot: Bot) -> None:
    heading("Bot account")
    try:
        me = await bot.get_me()
    except Exception as exc:
        fail(f"Cannot reach Telegram: {exc}", "check BOT_TOKEN and network access")
        return
    ok(f"Connected as @{me.username} (id {me.id})")

    if getattr(me, "can_join_groups", True) is False:
        fail(
            "Bot cannot join groups",
            "BotFather → /setjoingroups → Enable",
        )
    if getattr(me, "can_read_all_group_messages", None) is False:
        warn(
            "Privacy mode is ON - the bot sees only replies to its own messages "
            "and commands. This is the expected setting for the agreed design."
        )
    else:
        warn(
            "Privacy mode is OFF - the bot sees every message in every group it "
            "is in. Confirm NexterPay intended this."
        )


async def check_operations_group(bot: Bot, chat: Chat) -> None:
    label = f"{chat.department.label} Operations ({chat.telegram_chat_id})"
    try:
        info = await bot.get_chat(chat.telegram_chat_id)
    except Exception as exc:
        fail(
            f"{label}: cannot read the group ({exc})",
            "check the chat id, and that the bot is still a member",
        )
        return

    if not getattr(info, "is_forum", False):
        fail(
            f"{label}: topics are not enabled",
            "Group Settings → Topics → Enable. Work items cannot be created without this.",
        )
    else:
        ok(f"{label}: topics enabled")

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.telegram_chat_id, me.id)
    except Exception as exc:
        fail(f"{label}: cannot read bot membership ({exc})", "re-add the bot to the group")
        return

    if member.status != "administrator":
        fail(
            f"{label}: bot is '{member.status}', not an administrator",
            "promote the bot to admin in this group",
        )
        return

    if not getattr(member, "can_manage_topics", False):
        fail(
            f"{label}: bot lacks 'Manage Topics'",
            "edit the bot's admin rights and enable Manage Topics",
        )
    else:
        ok(f"{label}: bot is admin with Manage Topics")

    if not getattr(member, "can_delete_messages", False):
        warn(f"{label}: bot cannot delete messages (not required, but limits tidying up)")


async def check_client_group(bot: Bot, chat: Chat) -> None:
    label = f"client group {chat.telegram_chat_id}"
    try:
        info = await bot.get_chat(chat.telegram_chat_id)
    except Exception as exc:
        fail(f"{label}: cannot read the group ({exc})", "check the chat id and membership")
        return

    client_name = chat.client.name if chat.client else "unlinked"
    code = chat.client.code if chat.client else None
    if code:
        ok(f"{info.title or label}: {code} - {client_name} / {chat.department.value}")
    else:
        # Not fatal - requests still work and keep the older #1000 form - but
        # nothing can be filed against this counterparty until it has a code.
        warn(
            f"{info.title or label}: {client_name} has no four-letter code. "
            f"References will read #1000 rather than ACME-1000, and no request "
            f"can be filed under this counterparty. Set one with /np_setcode "
            f"inside their group."
        )

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.telegram_chat_id, me.id)

        if member.status not in ("administrator", "member"):
            fail(f"{label}: bot status is '{member.status}'", "re-add the bot to the group")
        elif member.status == "administrator":
            # NexterPay chose this on 30 August. Recorded rather than warned
            # about, but stated plainly, because it is the setting that decides
            # whether the bot can read a client's general conversation.
            ok(
                f"{info.title or label}: bot is an administrator - it receives every "
                "message in this group, which is what NexterPay asked for"
            )
        else:
            ok(
                f"{info.title or label}: bot is an ordinary member - it sees only "
                "commands and replies to its own messages"
            )
    except Exception as exc:
        warn(f"{label}: could not confirm bot membership ({exc})")

    # Topics in a client group are not part of the agreed design. Clients get
    # one plain conversation; the topic-per-request structure lives in the
    # Operations Group where staff work. A forum here means client messages
    # carry a thread id the bot does not track, and replies land wherever
    # Telegram decides rather than where the client is looking.
    if getattr(info, "is_forum", False):
        warn(
            f"{info.title or label}: Topics are ENABLED in this client group. "
            "Client groups should be ordinary groups - topics belong in the "
            "Operations Group. Turn them off in Group Settings, or expect "
            "replies to appear in the wrong place."
        )

    # Two bots in one group is the quiet killer. Under privacy mode a bare
    # /raise only reaches us "if the bot was the last bot to send a message to
    # the group" - so NexterPay's previous bot posting anything at all can
    # stop /raise working, with no error anywhere. We can only see admins via
    # the API, so this catches the common case, not every case.
    try:
        me = await bot.get_me()
        others = [
            a.user for a in await bot.get_chat_administrators(chat.telegram_chat_id)
            if getattr(a.user, "is_bot", False) and a.user.id != me.id
        ]
        if others:
            names = ", ".join(f"@{u.username or u.id}" for u in others)
            warn(
                f"{info.title or label}: another bot is an administrator here ({names}). "
                "Under privacy mode a plain /raise only reaches our bot if ours was "
                "the last bot to post. Remove the other bot, or tell clients to use "
                f"/raise@{me.username}."
            )
    except Exception as exc:
        warn(f"{label}: could not list administrators ({exc})")


async def check_staff_anonymity(bot: Bot, chat: Chat) -> None:
    """Flag human administrators of an Operations Group.

    Telegram turns on "Remain Anonymous" for group admins by default. An
    anonymous admin's messages arrive from the group rather than the person,
    so the bot cannot identify them and refuses every action. Staff only need
    to be members here.
    """
    try:
        admins = await bot.get_chat_administrators(chat.telegram_chat_id)
    except Exception as exc:
        warn(f"Could not list administrators of {chat.department.value} operations ({exc})")
        return

    people = [
        a for a in admins
        if getattr(a.user, "is_bot", False) is False and a.status != "creator"
    ]
    anonymous = [a for a in people if getattr(a, "is_anonymous", False)]

    if anonymous:
        names = ", ".join(a.user.full_name for a in anonymous)
        warn(
            f"{chat.department.label} Operations: {names} "
            f"{'are' if len(anonymous) > 1 else 'is'} an anonymous administrator. "
            "The bot cannot tell who they are and will refuse their commands. "
            "Dismiss them as Telegram admin, or turn off 'Remain Anonymous'."
        )
    elif people:
        ok(
            f"{chat.department.label} Operations: "
            f"{len(people)} human admin(s), none anonymous"
        )


async def check_registry() -> tuple[list[Chat], list[Chat]]:
    heading("Registered groups")
    async with session_scope() as session:
        result = await session.execute(select(Chat).where(Chat.is_active.is_(True)))
        chats = list(result.scalars().all())

        ops = [c for c in chats if c.kind is ChatKind.OPERATIONS]
        clients = []
        for c in chats:
            if c.kind is ChatKind.CLIENT:
                await session.refresh(c, ["client"])
                clients.append(c)

    if not ops:
        fail(
            "No Operations Groups registered",
            "run /register_ops <department> inside each internal group",
        )
    if not clients:
        fail(
            "No client groups registered",
            "run /register_client <department> <client name> inside each client group",
        )

    for chat in clients:
        matching = [o for o in ops if o.department is chat.department]
        if not matching:
            fail(
                f"Client group {chat.telegram_chat_id} is {chat.department.value}, "
                f"but no {chat.department.value} Operations Group exists",
                f"run /register_ops {chat.department.value} in the right internal group",
            )
    return ops, clients


async def check_staff() -> None:
    heading("Staff")
    async with session_scope() as session:
        result = await session.execute(select(Staff).where(Staff.is_active.is_(True)))
        staff = list(result.scalars().all())

    if not staff:
        fail(
            "No active staff registered",
            "reply to each person with /adduser <role> <department>",
        )
        return

    ok(f"{len(staff)} active staff")
    admins = [s for s in staff if s.role is StaffRole.ADMINISTRATOR]
    if not admins:
        warn(
            "No administrator registered - ADMIN_BOOTSTRAP_ID is still doing that job. "
            "Add a real administrator and clear the bootstrap id."
        )

    by_department: dict[str, int] = {}
    for person in staff:
        by_department[person.department.value] = by_department.get(person.department.value, 0) + 1
    for department, count in sorted(by_department.items()):
        ok(f"  {department}: {count}")


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        print("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
        raise SystemExit(2)

    init_engine(settings.database_url)
    bot = Bot(token=settings.bot_token)

    try:
        await check_bot(bot)
        ops, clients = await check_registry()

        if ops:
            heading("Operations Groups")
            for chat in ops:
                await check_operations_group(bot, chat)
                await check_staff_anonymity(bot, chat)

        if clients:
            heading("Client groups")
            for chat in clients:
                await check_client_group(bot, chat)

        await check_staff()
    finally:
        await bot.session.close()

    heading("Result")
    if problems:
        print(f"  {len(problems)} problem(s) will block testing:")
        for problem in problems:
            print(f"    - {problem}")
        raise SystemExit(1)

    if warnings:
        print(f"  Ready to test, with {len(warnings)} thing(s) worth confirming.")
    else:
        print("  Ready to test.")


if __name__ == "__main__":
    asyncio.run(main())
