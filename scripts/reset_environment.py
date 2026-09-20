"""Empty an environment: delete every topic, wipe every ticket, reset the numbering.

NexterPay, 20 September: "at end of testing to reset the clocks so all history
of tickets are wiped" and "i want a complete clean of old topics, but want to
keep everything else, whats safest way, this includes archive".

**Read this before running it.** This deletes things that cannot be recovered
from inside Telegram or from inside this platform. It exists as a script rather
than a bot command deliberately: a destructive action should not be one tap
away in a group where people are working.

What it removes
---------------
* Every forum topic the bot created — in the Operations Groups and in the
  archive groups. The conversations inside them go with them.
* Every work item, message, attachment, event, link, FX order and broadcast.
* The reference counters, back to their starting value, so the next request is
  #1000 again.

What it keeps
-------------
* The groups themselves and their registration — Operations, client, supplier
  and archive.
* Staff, their departments and their levels.
* Counterparties and their four-letter codes, unless ``--counterparties`` is
  given.

The order matters, and it is the one thing that cannot be got wrong
-------------------------------------------------------------------
Topics are deleted **first**, from ids held in the database, and the database
is wiped **second**.

The Bot API has no method to list the topics in a group — there is
``deleteForumTopic`` but nothing that enumerates them. The only record of which
topics exist is `work_items.topic_id` and `work_items.archive_topic_id`. Wipe
the database first and every topic in every group becomes permanently
unreachable: still there, still full of client conversations, and with nothing
left that knows their ids. They would have to be deleted by hand, one at a
time, for ever.

So: topics, then rows. Never the other way round.

What it cannot do
-----------------
* The General topic cannot be deleted by anybody — Telegram does not allow it.
  It can only be hidden, which `/npregisterarchive` already does for archives.
* A topic created by hand in Telegram was never recorded here, so it is not
  known and will not be touched.
* Messages sent into a client or supplier group are ordinary messages in an
  ordinary group, not topics. They stay. If those need to go, somebody clears
  the chat in Telegram.

Usage
-----
    python scripts/reset_environment.py                     # dry run, default
    python scripts/reset_environment.py --confirm "ERASE <db name>"
    python scripts/reset_environment.py --confirm "..." --counterparties

The confirmation phrase has to name the database it is pointed at, so that
running it against the wrong one requires typing the wrong one out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import delete, select, update

from app.config import get_settings
from app.db.base import init_engine, session_scope
from app.db.models import (
    Attachment,
    Broadcast,
    BroadcastDelivery,
    Chat,
    Client,
    Event,
    FxOrder,
    FxReferenceCounter,
    Message,
    ReferenceCounter,
    WorkItem,
    WorkItemLink,
)
from app.domain.enums import ChatKind
from app.services.gateway import AiogramGateway

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reset")

# Wiped, in an order that respects the foreign keys: children before parents.
#
# Listed explicitly rather than reflected from the metadata. A reflected list
# quietly grows when somebody adds a table, and "quietly grows" is the last
# property you want in the thing that deletes everything.
WIPE_IN_ORDER = [
    Attachment,
    Message,
    Event,
    WorkItemLink,
    BroadcastDelivery,
    Broadcast,
    FxOrder,
    WorkItem,
]

COUNTER_START = 1000


def _database_name() -> str:
    """The tail of the connection string, for the confirmation phrase."""
    return get_settings().database_url.rsplit("/", 1)[-1].split("?")[0]


async def _topics_to_delete(session) -> list[tuple[int, int, str]]:
    """(telegram_chat_id, thread_id, what it is) for every topic we created.

    Read before anything is deleted, because after the wipe this returns
    nothing and the topics are unreachable for ever.
    """
    chats = {
        chat.id: chat
        for chat in (await session.execute(select(Chat))).scalars().all()
    }
    found: list[tuple[int, int, str]] = []

    result = await session.execute(select(WorkItem))
    for item in result.scalars().all():
        ops = chats.get(item.operations_chat_id)
        if item.topic_id and ops is not None:
            found.append((ops.telegram_chat_id, item.topic_id, item.display_reference))
        if item.archive_topic_id:
            archive = next(
                (
                    chat for chat in chats.values()
                    if chat.kind is ChatKind.ARCHIVE
                    and chat.department is item.department
                ),
                None,
            )
            if archive is not None:
                found.append(
                    (
                        archive.telegram_chat_id,
                        item.archive_topic_id,
                        f"{item.display_reference} (archived)",
                    )
                )
    return found


async def run(*, confirm: str | None, counterparties: bool) -> int:
    settings = get_settings()
    init_engine(settings.database_url)

    expected = f"ERASE {_database_name()}"
    live = confirm == expected

    if confirm is not None and not live:
        logger.error("The confirmation phrase did not match. Expected: %s", expected)
        return 2

    if not live:
        logger.info("DRY RUN — nothing will be changed.")
        logger.info("To do this for real: --confirm %r\n", expected)

    async with session_scope() as session:
        topics = await _topics_to_delete(session)
        counts = {}
        for model in WIPE_IN_ORDER:
            counts[model.__tablename__] = len(
                (await session.execute(select(model.id))).scalars().all()
            )
        client_count = len(
            (await session.execute(select(Client.id))).scalars().all()
        )

    logger.info("Topics to delete:   %d", len(topics))
    for chat_id, thread_id, label in topics[:10]:
        logger.info("    %s  thread %s  in chat %s", label, thread_id, chat_id)
    if len(topics) > 10:
        logger.info("    ... and %d more", len(topics) - 10)

    logger.info("\nRows to delete:")
    for table, count in counts.items():
        logger.info("    %-22s %d", table, count)
    if counterparties:
        logger.info("    %-22s %d", "clients", client_count)

    logger.info("\nKept: groups and their registration, staff and their levels%s.",
                "" if counterparties else ", counterparties and their codes")
    logger.info("Reference counters reset to %d.", COUNTER_START)

    if not live:
        logger.info("\nNothing was changed.")
        return 0

    # ---- topics first, always ----
    gateway = AiogramGateway(_bot(settings))
    deleted = failed = 0
    for chat_id, thread_id, label in topics:
        try:
            await gateway.delete_topic(chat_id, thread_id)
            deleted += 1
        except Exception as exc:
            failed += 1
            logger.warning("Could not delete %s (thread %s): %s", label, thread_id, exc)

    logger.info("Topics deleted: %d, failed: %d", deleted, failed)
    if failed:
        # Deliberately not fatal, and deliberately loud. A topic we could not
        # delete is a topic that will still be there afterwards with nothing
        # pointing at it, so somebody needs to know which ones by hand.
        logger.warning(
            "Some topics could not be deleted. They will remain in Telegram and "
            "nothing will know their ids after this. Clear them by hand."
        )

    # ---- then the rows ----
    async with session_scope() as session:
        for model in WIPE_IN_ORDER:
            await session.execute(delete(model))
        if counterparties:
            await session.execute(delete(Client))
        for counter in (ReferenceCounter, FxReferenceCounter):
            await session.execute(update(counter).values(next_value=COUNTER_START))

    logger.info("Done. The next request will be #%d.", COUNTER_START)
    return 0


def _bot(settings):
    from aiogram import Bot

    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set; cannot delete topics.")
    return Bot(token=settings.bot_token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        help='The phrase "ERASE <database name>". Without it this is a dry run.',
    )
    parser.add_argument(
        "--counterparties",
        action="store_true",
        help="Also remove clients and suppliers. Their codes go with them.",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(confirm=args.confirm, counterparties=args.counterparties)
    )


if __name__ == "__main__":
    sys.exit(main())
