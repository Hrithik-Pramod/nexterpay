"""Put the traffic light and the new header onto requests that already exist.

    docker compose exec bot python scripts/refresh_topics.py            # dry run
    docker compose exec bot python scripts/refresh_topics.py --apply
    docker compose exec bot python scripts/refresh_topics.py --apply --closed

Why this is needed at all.

`refresh_header` runs when something about a request changes - it is claimed,
its status moves, it is closed. Requests raised before the deploy have not
changed since, so they keep the title and the header they were created with.
The consequence is specific and bad: NexterPay open their groups after an
upgrade, look at the work already in flight, and see none of what they asked
for. The feature is live and invisible, which reads exactly like a feature
that does not work.

Run once after deploying. It is safe to run again - every edit sets the title
and header to what they should be, so a second run changes nothing.

Dry run is the default. It prints the old title and the new one for every
request it would touch, and touches nothing, because a script that edits every
topic in every group is not one to point at production on trust.

Closed requests are skipped unless --closed is given. Their topics are
archived, and reopening the question of whether Telegram will accept an edit
to an archived topic is not worth doing by accident - the green light on a
closed request is also the least useful of the three.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import init_engine, session_scope  # noqa: E402
from app.db.models import Chat, Client, WorkItem  # noqa: E402
from app.domain.enums import WorkItemStatus  # noqa: E402
from app.services import relay  # noqa: E402
from app.services.gateway import AiogramGateway  # noqa: E402
from app.services.throttle import ThrottledGateway  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s")

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
DIM = "\033[2m"
OFF = "\033[0m"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually rewrite the topics. Without this, prints and exits.",
    )
    parser.add_argument(
        "--closed", action="store_true",
        help="include closed requests, whose topics are archived.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set")
    init_engine(settings.database_url)

    bot = Bot(token=settings.bot_token)
    gateway = ThrottledGateway(AiogramGateway(bot))

    async with session_scope() as session:
        stmt = select(WorkItem).where(WorkItem.topic_id.is_not(None))
        if not args.closed:
            stmt = stmt.where(WorkItem.status != WorkItemStatus.CLOSED)
        items = list((await session.execute(stmt)).scalars().all())

        # Sorted so the output reads in the same order as the topic list,
        # which is how someone will check it against Telegram afterwards.
        items.sort(key=lambda i: i.display_reference)

        if not items:
            print("Nothing to do - no requests with topics.")
            return

        print(f"{len(items)} request(s) to refresh"
              f"{'' if args.closed else ' (closed ones skipped - use --closed)'}\n")

        ops_titles: dict[int, str] = {}
        for chat in (await session.execute(select(Chat))).scalars().all():
            ops_titles[chat.id] = chat.title or str(chat.telegram_chat_id)

        failures: list[tuple[str, Exception]] = []
        for item in items:
            client = await session.get(Client, item.client_id)
            new_name = relay.topic_name(item, client.name if client else "Unknown client")
            print(f"  {new_name}")

            if not args.apply:
                continue

            try:
                await relay.refresh_header(session, gateway, item)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                # One topic deleted by hand in Telegram must not stop the
                # other thirty-three from being corrected.
                failures.append((item.display_reference, exc))
                print(f"    {YELLOW}could not refresh: {exc}{OFF}")

        if not args.apply:
            print(f"\n{DIM}Dry run. Nothing was changed. "
                  f"Add --apply to write these.{OFF}")
            return

        done = len(items) - len(failures)
        print(f"\n{GREEN}Refreshed {done} of {len(items)}.{OFF}")
        if failures:
            print(f"{YELLOW}{len(failures)} could not be refreshed:{OFF}")
            for reference, exc in failures:
                print(f"  {reference}: {exc}")
            print(f"\n{DIM}Usually a topic that was deleted in Telegram. "
                  f"The request itself is unaffected.{OFF}")

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
