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

from aiogram import Bot, Dispatcher
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


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

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
