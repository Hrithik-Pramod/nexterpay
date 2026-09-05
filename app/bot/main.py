"""Bot entrypoint.

Long polling by design. It needs no public domain, no TLS certificate and no
inbound firewall rules, and at NexterPay's volumes the latency difference is
irrelevant.

One consequence, and it is not optional: exactly one instance may run. Two
processes polling the same token both receive every update, which would
duplicate every relayed message. If a second instance is ever needed, that is
the point at which this moves to webhooks behind a load balancer, and the
rate limiter in `app.services.throttle` moves to Redis.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot import commands as cmd
from app.bot import deps
from app.bot.handlers import admin, broadcast, client, outbound, staff
from app.bot.help import build as build_help
from app.bot.registry import resolve_chat, resolve_staff
from app.bot.routing import build_strategy
from app.config import get_settings
from app.db.base import init_engine, session_scope
from app.domain.enums import ChatKind, StaffRole
from app.services.gateway import AiogramGateway
from app.services.throttle import ThrottledGateway

logger = logging.getLogger(__name__)


# What each role adds to the one below it. Written as what a person gains,
# not as a permission name, because this is read by someone who has just been
# refused something and wants to know why.
_ROLE_GRANTS = {
    StaffRole.OPERATOR:
        "claim, reply, note, set status and priority, file under a supplier, "
        "link requests, close",
    StaffRole.SENIOR_OPERATOR: "everything an Operator can, plus reassign and escalate",
    StaffRole.MANAGER:
        "everything a Senior Operator can, plus reopen a closed request and broadcast",
    # Two different things, and running them together is what made this
    # confusing in the group. Administration - registering a group, adding a
    # person - is not tied to a desk at all. Seniority is, and being an
    # administrator on Support does not make you a manager on Finance. The
    # bot refuses on exactly that basis, so the wording has to match, or
    # somebody reads "not limited to one department" and concludes the
    # refusal is a fault.
    StaffRole.ADMINISTRATOR:
        "everything a Manager can on this desk, plus registering groups and "
        "managing staff — and those two work in any group, not just this one",
}


def whoami_text(person) -> str:
    """What `/npwhoami` replies.

    Pulled out of the handler and given a test because of what the last line
    means. After the migration that split a person's single department into a
    set of desks, this command is how anyone checks their own record survived
    - and "registered, but not on any department" is the sentence that says it
    did not. A message that only appears when something has gone wrong is
    exactly the one that is never exercised until the day it matters.

    It also spells out what each role permits. Somebody runs this because
    something was refused and they want to know whether that was their role,
    their department, or a fault - and a role name on its own answers none of
    those.
    """
    desks = [
        f"{m.department.label} — {m.role.value.replace('_', ' ')}\n"
        f"    {_ROLE_GRANTS[m.role]}"
        for m in person.desks
    ]
    if not desks:
        return f"{person.display_name} — registered, but not on any department."
    return (
        f"{person.display_name}\n"
        + "\n".join(desks)
        + "\n\nSeniority is held per department, so it does not carry across."
    )


def whoami_response(chat, person) -> str:
    """What `/npwhoami` replies, given where it was sent and by whom.

    Separated from the handler so it can be tested by calling it. The first
    attempt at this guard was tested by reading the handler's source for the
    right words, and that test passed against a version with the condition
    disabled - the words were all still there. A test that inspects code
    rather than running it will accept anything that looks right.

    `chat is None` - an unregistered group - is treated as unsafe. We have no
    idea who is in it.
    """
    if chat is None or chat.kind is not ChatKind.OPERATIONS:
        return (
            "Not in here - this group may have people from outside NexterPay "
            "in it, and the answer names departments and seniority.\n\n"
            f"Send /{cmd.WHOAMI} in an Operations Group, or /{cmd.HELP} here "
            f"for what you can do in this group."
        )
    if person is None:
        return "You are not registered as NexterPay staff."
    return whoami_text(person)


def build_storage():
    """Keep half-finished requests alive across a restart.

    A client taps "Raise Request", gets distracted, and answers ten minutes
    later - by which time we may have redeployed. With in-memory state that
    reply lands with no state attached and is silently dropped: the client
    sees nothing, and the log shows nothing, because as far as the bot is
    concerned no request was ever started. Redis is already running for the
    rate limiter, so this costs nothing.

    Falls back to memory rather than refusing to start; a bot with forgetful
    state is worth more than a bot that is down.
    """
    settings = get_settings()

    # Probe the socket first. RedisStorage.from_url() only builds a client -
    # it does not connect, so wrapping it in try/except catches nothing. The
    # first connection attempt happens inside aiogram's FSM middleware, which
    # runs on EVERY update before any handler. An unreachable Redis therefore
    # does not degrade the bot, it stops it dead: polling continues, the logs
    # fill with ConnectionError, and not one message is answered.
    if not _reachable(settings.redis_url):
        logger.warning(
            "Redis at %s is unreachable - using in-memory FSM state. "
            "Half-finished requests will not survive a restart.",
            settings.redis_url,
        )
        return MemoryStorage()

    from aiogram.fsm.storage.redis import RedisStorage

    logger.info("FSM state in Redis (%s)", settings.redis_url)
    return RedisStorage.from_url(settings.redis_url)


def _reachable(url: str, timeout: float = 3.0) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 6379), timeout=timeout
        ):
            return True
    except OSError:
        return False


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=build_storage())


    @dp.message(cmd.any_case(cmd.START, cmd.START_ALIAS))
    async def cmd_start(message: Message) -> None:
        async with session_scope() as session:
            chat = await resolve_chat(session, message.chat.id)
        if chat is None:
            await message.reply(
                "NexterPay Operations Bot is online, but this group is not "
                "registered. An administrator must register it first."
            )
            return
        await message.reply(
            f"NexterPay Operations Bot is online.\n"
            f"Group: {chat.kind.value} / {chat.department.value}"
        )

    @dp.message(cmd.any_case(cmd.HELP))
    async def cmd_help(message: Message) -> None:
        """What you can do, from where you are standing.

        Registered on the dispatcher rather than in a router, so it answers
        in a client group, an Operations Group and an unregistered one alike -
        the three places somebody is most likely to be lost.
        """
        async with session_scope() as session:
            chat = await resolve_chat(session, message.chat.id)
            role, is_administrator = None, False
            if message.from_user is not None:
                person = await resolve_staff(session, message.from_user.id)
                if person is not None:
                    is_administrator = person.is_administrator
                    if chat is not None:
                        role = person.role_in(chat.department)
            text = build_help(chat, role, is_administrator=is_administrator)

        await message.reply(text)

    @dp.message(cmd.any_case(cmd.WHOAMI))
    async def cmd_whoami(message: Message) -> None:
        """Your desks and your seniority - but not in front of a counterparty.

        This answered anywhere, including inside a client or supplier group,
        and NexterPay found it: on 4 September a member of staff ran it in the
        Pexi supplier group and the bot published his departments and his role
        on each into a room the supplier is sitting in.

        Nothing here is a secret, exactly. It is still internal structure -
        who covers which desk, and who outranks whom - and a supplier reading
        it learns something about how NexterPay is organised that nobody chose
        to tell them. The command is for the person, not the room, so outside
        an Operations Group it says where to go instead.
        """
        if message.from_user is None:
            return
        async with session_scope() as session:
            chat = await resolve_chat(session, message.chat.id)
            person = await resolve_staff(session, message.from_user.id)

        await message.reply(whoami_response(chat, person))

    # Order matters. Admin commands first, then staff (Operations Groups),
    # then client. The client router ends in a catch-all, so it goes last.
    dp.include_router(admin.router)
    # Before staff: composing a broadcast is answered as a reply, which in a
    # forum carries a thread id and would otherwise be eaten by the staff
    # topic catch-all - the same trap that hid the client-reply bug.
    dp.include_router(broadcast.router)
    dp.include_router(outbound.router)
    dp.include_router(staff.router)
    dp.include_router(client.router)

    # Included last, so it only sees what every other handler declined.
    #
    # During UAT the hardest report to act on is "it did nothing". This turns
    # that into evidence: if a line appears here, Telegram delivered the
    # message and we chose to ignore it. If no line appears, Telegram never
    # delivered it at all - almost always privacy mode. Two very different
    # bugs that look identical from the group.
    trace = Router(name="trace")

    @trace.message()
    async def _unhandled(message: Message) -> None:
        # Deliberately does not log what the message said.
        #
        # This was invaluable while the bot could only see replies to itself.
        # Once it is an administrator in client groups it sees everything, and
        # logging content would put fragments of clients' private conversation
        # into the server logs - which NexterPay have not agreed to and neither
        # of us intended.
        #
        # A leading command is kept, because commands are not private and are
        # the thing worth diagnosing. Everything else is reduced to a length.
        body = (message.text or message.caption or "")
        command = body.split()[0] if body.startswith("/") else None
        logger.info(
            "unhandled message chat=%s (%s) user=%s thread=%s reply=%s command=%s chars=%d",
            message.chat.id,
            message.chat.type,
            message.from_user.id if message.from_user else None,
            message.message_thread_id,
            bool(message.reply_to_message),
            command,
            len(body),
        )

    dp.include_router(trace)
    return dp


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set - copy .env.example to .env and fill it in")

    init_engine(settings.database_url, echo=settings.debug_sql)

    strategy = build_strategy(settings.reply_routing_strategy)
    logger.info("Reply routing strategy: %s", strategy.name)

    bot = Bot(token=settings.bot_token)
    deps.set_gateway(ThrottledGateway(AiogramGateway(bot)))

    me = await bot.get_me()
    logger.info("Starting as @%s", me.username)

    dp = build_dispatcher()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
