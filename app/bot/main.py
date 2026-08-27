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
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot import deps
from app.bot.handlers import admin, client, staff
from app.bot.registry import resolve_chat, resolve_staff
from app.bot.routing import build_strategy
from app.config import get_settings
from app.db.base import init_engine, session_scope
from app.services.gateway import AiogramGateway
from app.services.throttle import ThrottledGateway

logger = logging.getLogger(__name__)


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


    @dp.message(Command("start"))
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

    @dp.message(Command("whoami"))
    async def cmd_whoami(message: Message) -> None:
        if message.from_user is None:
            return
        async with session_scope() as session:
            person = await resolve_staff(session, message.from_user.id)
        if person is None:
            await message.reply("You are not registered as NexterPay staff.")
            return
        await message.reply(
            f"{person.display_name} — {person.role.value}, {person.department.value}"
        )

    # Order matters. Admin commands first, then staff (Operations Groups),
    # then client. The client router ends in a catch-all, so it goes last.
    dp.include_router(admin.router)
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
        logger.info(
            "unhandled message chat=%s (%s) user=%s thread=%s reply=%s text=%r",
            message.chat.id,
            message.chat.type,
            message.from_user.id if message.from_user else None,
            message.message_thread_id,
            bool(message.reply_to_message),
            (message.text or message.caption or "")[:80],
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
