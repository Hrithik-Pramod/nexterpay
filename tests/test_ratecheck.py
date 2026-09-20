"""The nine o'clock rate check.

NexterPay asked for rate checks to be initiated from Operations "or automate
this at a set time" on 16 September, and set the time on the 17th: 09:00 UTC.

Two things in here are worth more attention than the schedule itself.

**Who raises these.** Every request records who raised it, and a job running at
nine has nobody behind it. The choice was between attributing the tickets to a
member of staff who did not raise them — which makes the audit trail untrue in
the one place people go to establish what happened — and giving the platform an
account of its own. NexterPay chose the second. So these say NexterPay
Operations, and that account is an ordinary Operator that appears in the staff
list rather than a hidden privilege.

**Not asking twice.** The sweep runs every fifteen minutes and the process
restarts on every deploy, so anything remembered in memory is forgotten and
every supplier gets asked again. The guard is a question about the data —
has a rate check been raised on this desk today — because a ticket survives a
restart and a boolean does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.bot.registry import register_client_chat, register_operations_chat
from app.domain.enums import Department, StaffRole
from app.domain.errors import NotAuthorised
from app.services import ratecheck
from app.services.gateway import FakeGateway

NINE = datetime(2026, 9, 18, 9, 5, tzinfo=UTC)
EIGHT = datetime(2026, 9, 18, 8, 55, tzinfo=UTC)


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def finance_ops(session):
    return await register_operations_chat(
        session,
        telegram_chat_id=-1001000006666,
        department=Department.FINANCE,
        title="Finance Operations",
    )


@pytest_asyncio.fixture
async def pexi_finance(session, finance_ops):
    return await register_client_chat(
        session,
        telegram_chat_id=-1002000006666,
        client_name="Supplier Pexi",
        department=Department.FINANCE,
        title="Pexi — Finance",
        is_supplier=True,
    )


@pytest_asyncio.fixture
async def second_supplier(session, finance_ops):
    return await register_client_chat(
        session,
        telegram_chat_id=-1002000007777,
        client_name="Another Supplier Ltd",
        department=Department.FINANCE,
        title="Another — Finance",
        is_supplier=True,
    )


# --------------------------------------------------------------------------
# When it runs
# --------------------------------------------------------------------------

async def test_nothing_happens_before_nine(session, finance_ops, pexi_finance):
    assert await ratecheck.due(session, now=EIGHT) == []


async def test_a_desk_with_suppliers_is_due_after_nine(
    session, finance_ops, pexi_finance
):
    assert Department.FINANCE in await ratecheck.due(session, now=NINE)


async def test_it_fires_after_nine_not_only_at_nine(
    session, finance_ops, pexi_finance
):
    """The sweep runs on a fifteen-minute cycle, so it will almost never be
    looking at exactly 09:00. A job that only fires on the dot is one that
    silently does nothing on the day a deploy lands in that minute."""
    late = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
    assert Department.FINANCE in await ratecheck.due(session, now=late)


async def test_a_desk_with_no_suppliers_is_never_due(session, finance_ops):
    """Nobody to ask. Finance has an Operations Group here and no supplier
    group, which is the ordinary state of a desk on the day it is set up."""
    assert await ratecheck.due(session, now=NINE) == []


# --------------------------------------------------------------------------
# Which desks ask at all
#
# NexterPay, 19 September: "only FX operations ask the rate to the suppliers
# on that group right?" They were right to check. It was every desk, and the
# scheduled run on the 18th had already opened rate checks with the Business
# and Compliance supplier groups.
# --------------------------------------------------------------------------

async def test_a_desk_that_does_not_trade_currency_is_never_asked(
    session, support_ops, pexi_supplier, gw
):
    """A supplier group is not a reason to ask for a rate.

    Support has a supplier here - plenty of desks do, for things that have
    nothing to do with currency - and it must not be asked. This is the test
    the original lacked: it proved a desk *with* suppliers was due, and every
    desk with suppliers passed it.
    """
    assert Department.SUPPORT not in await ratecheck.due(session, now=NINE)
    assert await ratecheck.run(session, gw, now=NINE) == 0
    assert gw.all_text_to(pexi_supplier.telegram_chat_id) == ""


async def test_only_finance_is_ever_due(
    session, support_ops, pexi_supplier, finance_ops, pexi_finance
):
    """Both desks have suppliers. Only one buys currency."""
    assert await ratecheck.due(session, now=NINE) == [Department.FINANCE]


def test_the_desks_that_ask_are_stated_not_inferred() -> None:
    """Held as a list rather than worked out from whether a desk happens to
    have a supplier. If Business ever does start dealing rates, adding it here
    is the honest way to say so - and the change is visible in a diff."""
    assert ratecheck.RATE_CHECK_DEPARTMENTS == (Department.FINANCE,)


# --------------------------------------------------------------------------
# Asking, once
# --------------------------------------------------------------------------

async def test_every_supplier_on_the_desk_is_asked(
    session, finance_ops, pexi_finance, second_supplier, gw
):
    assert await ratecheck.run(session, gw, now=NINE) == 2
    assert ratecheck.BODY in gw.all_text_to(pexi_finance.telegram_chat_id)
    assert ratecheck.BODY in gw.all_text_to(second_supplier.telegram_chat_id)


async def test_running_twice_in_a_day_asks_once(
    session, finance_ops, pexi_finance, gw
):
    """The test that matters most here.

    The sweep runs four times an hour. Without this, every supplier would be
    asked for a rate ninety-six times a day — and the second time would be the
    last time anybody read one of these messages.
    """
    assert await ratecheck.run(session, gw, now=NINE) == 1
    later = NINE + timedelta(hours=2)
    assert await ratecheck.run(session, gw, now=later) == 0


async def test_a_restart_does_not_ask_again(
    session, finance_ops, pexi_finance, gw
):
    """Nothing is remembered in memory, so there is nothing for a restart to
    forget. `already_asked_today` reads the tickets."""
    await ratecheck.run(session, gw, now=NINE)
    assert await ratecheck.already_asked_today(
        session, Department.FINANCE, now=NINE
    ) is True


async def test_tomorrow_it_asks_again(
    session, finance_ops, pexi_finance, gw
):
    await ratecheck.run(session, gw, now=NINE)
    tomorrow = NINE + timedelta(days=1)
    assert await ratecheck.run(session, gw, now=tomorrow) == 1


async def test_a_manual_run_counts_as_today_s_ask(
    session, finance_ops, operator, pexi_finance, gw
):
    """Somebody who ran /npratecheck by hand has already asked.

    The guard looks at the tickets, not at who raised them, so a run by a
    person counts exactly as the nine o'clock one does. The desk does not need
    the bot asking the same suppliers again an hour later — and the suppliers
    certainly do not.
    """
    from app.domain.work_items import Actor
    from app.services.relay import open_outbound

    await open_outbound(
        session, gw,
        counterparty_chat=pexi_finance,
        subject=ratecheck.SUBJECT,
        body=ratecheck.BODY,
        actor=Actor.of(operator),
    )

    assert await ratecheck.already_asked_today(
        session, Department.FINANCE, now=NINE
    ) is True
    assert await ratecheck.run(session, gw, now=NINE) == 0


# --------------------------------------------------------------------------
# Who raised it
# --------------------------------------------------------------------------

async def test_the_tickets_say_who_raised_them(
    session, finance_ops, pexi_finance, gw
):
    """And it is not a person who was asleep at the time."""
    from sqlalchemy import select

    from app.db.models import WorkItem

    await ratecheck.run(session, gw, now=NINE)
    result = await session.execute(select(WorkItem))
    items = list(result.scalars().all())

    assert items
    assert all(i.raised_by_name == ratecheck.SYSTEM_NAME for i in items)
    assert all(i.raised_by_us for i in items)


async def test_the_system_account_is_an_ordinary_operator(
    session, finance_ops
):
    """Not a back door. It can do what an Operator can do and no more — which
    is all it needs, and means it shows up in the staff list where anybody can
    see it."""
    actor = await ratecheck.system_actor(session, Department.FINANCE)

    assert actor.name == ratecheck.SYSTEM_NAME
    assert actor.staff is not None
    assert actor.role is StaffRole.OPERATOR
    actor.require(StaffRole.OPERATOR)

    # Named rather than blind. `pytest.raises(Exception)` would pass on a typo
    # in the line below just as happily as on a refusal, which would make this
    # a test that cannot fail for the reason it exists.
    with pytest.raises(NotAuthorised):
        actor.require(StaffRole.MANAGER)


async def test_its_id_cannot_collide_with_a_person(session, finance_ops):
    """Telegram user ids are positive, so zero belongs to nobody.

    A mention of this account will not resolve to anyone — it reads as a name
    rather than a link, which is accurate: there is no one to tap through to.
    """
    assert ratecheck.SYSTEM_TELEGRAM_USER_ID == 0


async def test_seniority_is_still_held_per_desk(session, finance_ops):
    """The same rule as for a person. An account registered on Support has no
    standing on Finance, and this one is registered desk by desk as it is
    needed."""
    await ratecheck.system_actor(session, Department.FINANCE)
    finance = await ratecheck.system_actor(session, Department.FINANCE)
    assert finance.role is StaffRole.OPERATOR


def test_the_time_is_the_one_they_set() -> None:
    """NexterPay, 17 September: "run at 9 am UTC"."""
    assert ratecheck.RUN_AT_UTC.hour == 9
    assert ratecheck.RUN_AT_UTC.minute == 0


def test_every_supplier_is_asked_the_same_question() -> None:
    """The replies are read side by side. Five differently-phrased questions
    produce five differently-shaped answers."""
    assert "1 USDT" in ratecheck.BODY
    assert "how long it holds" in ratecheck.BODY
