"""Asking every supplier for a rate, on a timer.

**Not currently scheduled. Deferred to Phase 2 on 20 September.**

NexterPay: "remove for now, as phase 2 item", alongside the supplier and
currency catalogue it belongs with. Asking every supplier the same question at
nine is only half of it; the other half is knowing which currency each of them
deals in, and that catalogue does not exist yet.

Nothing here is deleted and the tests still run. It is simply not wired to
anything - `app/bot/main.py` no longer calls `run()`. Re-enabling is restoring
one call, not rebuilding the feature, which is the state a deferred thing
should be left in.

`/npratecheck` is unaffected and still works. It has its own implementation in
the FX handlers and never used this module.

NexterPay, 16 September: "In Operations, we need to be able to initiate rate
checks, or automate this at a set time." The manual half is `/npratecheck`.
This was the timer, and they set it on the 17th: **09:00 UTC**.

**The identity question, and why it needed asking.** Every request on this
platform records who raised it, and a job that runs at nine in the morning has
nobody behind it. There were two ways out: attribute the tickets to a member of
staff who did not raise them, which makes the history untrue in the one place
people go when they are trying to establish what happened; or give the platform
an account of its own. NexterPay chose the second, so these requests are raised
by **NexterPay Operations** and say so.

That account is not a back door. It is an ordinary Staff row with Operator
level on the desks it covers, it appears in the staff list where anybody can
see it, and it can do exactly what an Operator can do - nothing here needs more
than that.

**Not asking twice is a property of the data, not of a flag.** The sweep that
calls this runs every fifteen minutes, and a restart clears anything held in
memory. So rather than remembering that today's run happened, it asks the
question that actually matters: has a rate check already been raised on this
desk today? A ticket is a fact that survives a restart, which a boolean is not.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.registry import upsert_staff
from app.db.models import Chat, WorkItem
from app.domain.enums import ChatKind, Department, StaffRole
from app.domain.work_items import Actor
from app.services.gateway import TelegramGateway
from app.services.relay import open_outbound

logger = logging.getLogger(__name__)

# NexterPay, 17 September: "run at 9 am UTC".
#
# UTC rather than local. A rate is a time-sensitive thing and the desks are not
# all in one place; a job that moved with the clocks would ask at a different
# moment twice a year, and nobody would connect the two.
RUN_AT_UTC = time(9, 0)

# The account these requests are raised by.
#
# Telegram user ids are positive, so zero belongs to nobody and cannot collide
# with a real person. Worth knowing that a mention of this account will not
# resolve to anyone - it appears as a name rather than a link, which is
# accurate: there is no one to tap through to.
SYSTEM_TELEGRAM_USER_ID = 0
SYSTEM_NAME = "NexterPay Operations"

# The desks that buy currency, and therefore the desks with rates to ask about.
#
# This used to be every department that happened to have a supplier group, and
# NexterPay caught it on 19 September: "only FX operations ask the rate to the
# suppliers on that group right?" They were right, and it had already happened
# - on the 18th the scheduled run opened SPEX-1085 in the Business supplier
# group and SPEX-1086 in Compliance. Neither desk trades currency. Both had a
# supplier, which was the whole of the test.
#
# A tuple rather than a flag on the department, because this is a fact about
# what a desk does rather than a setting somebody should be able to change by
# accident - and if Business ever does start dealing rates, adding it here is
# the honest way to say so.
RATE_CHECK_DEPARTMENTS: tuple[Department, ...] = (Department.FINANCE,)

SUBJECT = "Rate check"
BODY = (
    "Could you send your current rate — local currency per 1 USDT — and how "
    "long it holds?"
)


async def system_actor(session: AsyncSession, department: Department) -> Actor:
    """The platform acting as itself, on one desk.

    Registered per department because seniority on this platform is held per
    desk - an account with a role on Support has none on Finance, and that is
    true of this one exactly as it is of a person.
    """
    staff = await upsert_staff(
        session,
        telegram_user_id=SYSTEM_TELEGRAM_USER_ID,
        display_name=SYSTEM_NAME,
        role=StaffRole.OPERATOR,
        department=department,
    )
    return Actor.of(staff, department)


async def _supplier_groups(session: AsyncSession, department: Department) -> list[Chat]:
    result = await session.execute(
        select(Chat).where(
            Chat.is_active.is_(True),
            Chat.kind == ChatKind.CLIENT,
            Chat.is_supplier.is_(True),
            Chat.department == department,
        ).order_by(Chat.title)
    )
    return list(result.scalars().all())


async def already_asked_today(
    session: AsyncSession, department: Department, *, now: datetime
) -> bool:
    """Has a rate check already gone out on this desk today?

    The idempotency key, and it is a ticket rather than a flag on purpose. The
    sweep runs every fifteen minutes and the process restarts whenever we
    deploy; anything held in memory would be forgotten and every supplier would
    be asked again. A request that exists is a fact that survives both.

    Counts manual runs too. Somebody who ran `/npratecheck` at half past eight
    has already asked, and the desk does not need the bot asking again at nine.
    """
    midnight = datetime.combine(now.date(), time(0, 0), tzinfo=UTC)
    result = await session.execute(
        select(WorkItem.id).where(
            WorkItem.department == department,
            WorkItem.subject == SUBJECT,
            WorkItem.raised_by_us.is_(True),
            WorkItem.created_at >= midnight,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def due(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Department]:
    """Desks that should be asked now: past nine, trade currency, with
    suppliers, and not yet asked today.

    Past nine rather than at nine. The sweep runs on a fifteen-minute cycle, so
    it will rarely be looking at exactly 09:00 - and a job that only fires on
    the dot is a job that silently does nothing on the day a deploy happens to
    land in that minute.

    The department filter is first because it is the cheapest and because
    getting it wrong is the loudest: the failure mode is not an error, it is a
    supplier on a desk that does not trade currency being asked every morning
    for a rate nobody wants, until they stop reading anything we send them.
    """
    now = now or datetime.now(UTC)
    if now.timetz().replace(tzinfo=None) < RUN_AT_UTC:
        return []

    out = []
    for department in RATE_CHECK_DEPARTMENTS:
        if not await _supplier_groups(session, department):
            continue
        if await already_asked_today(session, department, now=now):
            continue
        out.append(department)
    return out


async def run(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    now: datetime | None = None,
) -> int:
    """One pass. Returns how many suppliers were asked.

    A failure on one supplier does not stop the rest - a desk would rather have
    four rates and one named problem than no rates and one error.
    """
    asked = 0
    for department in await due(session, now=now):
        actor = await system_actor(session, department)
        for chat in await _supplier_groups(session, department):
            try:
                item = await open_outbound(
                    session, gateway,
                    counterparty_chat=chat,
                    subject=SUBJECT,
                    body=BODY,
                    actor=actor,
                )
                asked += 1
                logger.info(
                    "Rate check %s opened with %s",
                    item.display_reference, chat.title or chat.telegram_chat_id,
                )
            except Exception:
                logger.exception(
                    "Rate check failed for %s", chat.title or chat.telegram_chat_id
                )
    return asked
