"""The outstanding book: what to chase, oldest first.

NexterPay's FX desk, through Jason on 2 October. His description of the job is
worth quoting, because it says where the pain is and it is not where this
project had assumed:

    He gets a message from a client asking about rate / he answers freeformat /
    he jumps on the supplier group, ask a specific person for the rate / he
    negotiates ... After that, he has to keep track of all the outstanding
    orders, keep reauditing and following up, and updating his list, whilst
    dealing with client chasers and supplier chasing.

Everything before "after that" he never calls painful. It is his craft, and he
is good at it. The platform had put eight states and a button tap around the
part he enjoys, and almost nothing around the part that hurts.

So this is the part that hurts, and the two facts it turns on are whose move
it is and how long it has been theirs. `/npfx` has neither as an organising
idea - it answers "what is live", flat and in reference order, which is a list
you have to read all of. The book answers "what do I do next".

The margin rule applies here harder than anywhere else in the FX layer. Every
line carries `display_reference`, which holds the client's code *and* the
supplier's - the two things `client_reference` and `supplier_reference` exist
to keep apart. That is correct for the Operations Group and catastrophic
anywhere else, so the last test in this file is structural rather than
behavioural.
"""

from __future__ import annotations

import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bot.handlers import fx as handlers
from app.db.base import utcnow
from app.db.models import Client, Event
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor


async def _deal(session, acme_support, operator, *, subject="EUR to XOF"):
    """A client request with a deal opened against it."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject=subject,
        original_message="Please provide a rate.",
        raised_by_name="Gavs D",
    )
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    return item, order


async def _events_for(session, order):
    result = await session.execute(
        select(Event).where(Event.work_item_id == order.client_work_item_id)
    )
    return [
        event for event in result.scalars().all()
        if (event.payload or {}).get("fx_reference") == order.display_reference
    ]


async def _age(session, order, days):
    """Push this deal's last movement into the past.

    Written against the event log rather than a column, because that is where
    `outstanding_book` reads from and a helper that cheated would be testing
    something the code does not do.
    """
    when = utcnow() - timedelta(days=days)
    for event in await _events_for(session, order):
        event.created_at = when
    await session.flush()


# --------------------------------------------------------------------------
# What goes in it
# --------------------------------------------------------------------------

async def test_an_empty_desk_has_an_empty_book(session):
    assert await fx.outstanding_book(session) == []


async def test_a_live_deal_is_in_the_book(session, acme_support, support_ops, operator):
    _, order = await _deal(session, acme_support, operator)

    book = await fx.outstanding_book(session)

    assert [entry.order.id for entry in book] == [order.id]


async def test_a_closed_deal_is_not(session, acme_support, support_ops, operator):
    """The book is the outstanding book. A finished deal is somebody's
    history, not somebody's homework."""
    _, order = await _deal(session, acme_support, operator)
    order.status = FxOrderStatus.CLOSED
    await session.flush()

    assert await fx.outstanding_book(session) == []


# --------------------------------------------------------------------------
# Ageing
# --------------------------------------------------------------------------

async def test_the_book_is_sorted_oldest_first(
    session, acme_support, support_ops, operator
):
    """The order of the list is the order to work it in.

    Sorting by reference would be the obvious thing and it would be wrong: the
    deal that needs chasing is the one nobody has touched, and that has no
    relationship to when it was numbered.
    """
    _, recent = await _deal(session, acme_support, operator, subject="recent")
    _, old = await _deal(session, acme_support, operator, subject="old")
    await _age(session, old, days=9)

    book = await fx.outstanding_book(session)

    assert [entry.order.id for entry in book] == [old.id, recent.id]


async def test_age_is_measured_from_the_last_movement(
    session, acme_support, support_ops, operator
):
    """Not from when the deal was opened.

    A deal opened three weeks ago and quoted this morning is not three weeks
    stale - it is waiting on a client who has had it for an hour. Measuring
    from creation would fill the top of the book with deals that are being
    worked perfectly well, which is the fastest way to teach somebody to
    ignore a chase list.
    """
    _, order = await _deal(session, acme_support, operator)
    await _age(session, order, days=20)

    # Something happens today.
    await fx.record_event(
        session, order, fx.EventType.FX_RATE_REQUESTED, Actor.of(operator)
    )

    book = await fx.outstanding_book(session)
    assert book[0].age().days == 0


async def test_a_deal_with_no_events_ages_from_its_request(
    session, acme_support, support_ops, operator
):
    """The honest fallback.

    Falling back to "now" would silently reset the age of anything the event
    log cannot explain, which means the one deal whose history is broken is
    also the one deal that never appears at the top of the chase list.
    """
    item, order = await _deal(session, acme_support, operator)
    for event in await _events_for(session, order):
        await session.delete(event)
    item.created_at = utcnow() - timedelta(days=4)
    await session.flush()

    book = await fx.outstanding_book(session)
    assert book[0].age().days == 4


@pytest.mark.parametrize(
    "days, stale, overdue",
    [(0, False, False), (3, True, False), (6, True, True)],
)
async def test_the_thresholds(
    session, acme_support, support_ops, operator, days, stale, overdue
):
    _, order = await _deal(session, acme_support, operator)
    await _age(session, order, days=days)

    entry = (await fx.outstanding_book(session))[0]
    assert entry.is_stale() is stale
    assert entry.is_overdue() is overdue


# --------------------------------------------------------------------------
# A deal out for a quote with nobody asked
#
# Found by running the book against real deals on 3 October, which is the
# whole argument for building it: FXACME-1002 sat under "waiting on suppliers"
# for five days with no supplier on it. `open_order` sets RATE_REQUESTED the
# moment a client asks, which is before anybody has chosen who to ask.
#
# The status is honest in general - the next thing that happens is a supplier
# quoting us. On a chase list it is a lie, and an expensive one: a deal nobody
# is working looks exactly like a deal somebody owes us an answer on.
# --------------------------------------------------------------------------

async def test_a_deal_with_no_supplier_is_ours_to_move(
    session, acme_support, support_ops, operator
):
    _, order = await _deal(session, acme_support, operator)
    assert order.status is FxOrderStatus.RATE_REQUESTED
    assert order.supplier_code is None

    entry = (await fx.outstanding_book(session))[0]

    assert order.status.waiting_on == "Supplier"   # the status is unchanged
    assert entry.waiting_on == "NexterPay"         # the book disagrees, on purpose
    assert entry.needs_a_supplier


async def test_once_a_supplier_is_chosen_it_is_theirs(
    session, acme_support, support_ops, operator
):
    """The correction must not swallow the ordinary case."""
    _, order = await _deal(session, acme_support, operator)
    order.supplier_code = "SPEX"
    await session.flush()

    entry = (await fx.outstanding_book(session))[0]
    assert entry.waiting_on == "Supplier"
    assert not entry.needs_a_supplier


async def test_the_line_says_what_the_next_move_is(
    session, acme_support, support_ops, operator
):
    """"Rate requested" on its own reads like we are owed an answer. The desk
    needs to see that we owe a question."""
    _, order = await _deal(session, acme_support, operator)

    entry = (await fx.outstanding_book(session))[0]
    assert "no supplier asked" in handlers.book_line(entry, utcnow())


async def test_it_is_filed_under_ours_rather_than_suppliers(
    session, acme_support, support_ops, operator
):
    _, order = await _deal(session, acme_support, operator)

    text = handlers.book_text(await fx.outstanding_book(session), utcnow())

    assert "Ours to move" in text
    assert "Waiting on suppliers" not in text


# --------------------------------------------------------------------------
# How it reads
# --------------------------------------------------------------------------

def test_age_text_is_short_enough_to_scan():
    assert handlers.age_text(timedelta(minutes=12)) == "new"
    assert handlers.age_text(timedelta(hours=5)) == "5h"
    assert handlers.age_text(timedelta(days=3, hours=4)) == "3d"


async def test_the_book_groups_by_whose_move_it_is(
    session, acme_support, support_ops, operator
):
    _, ours = await _deal(session, acme_support, operator, subject="ours")
    ours.status = FxOrderStatus.RATE_REJECTED          # waiting on NexterPay
    _, theirs = await _deal(session, acme_support, operator, subject="theirs")
    theirs.status = FxOrderStatus.RATE_QUOTED          # waiting on the client
    await session.flush()

    text = handlers.book_text(await fx.outstanding_book(session), utcnow())

    assert "Ours to move" in text
    assert "Waiting on clients" in text
    # Our own move leads. A chase list that opens with somebody else's
    # homework buries the deals nobody else is going to progress.
    assert text.index("Ours to move") < text.index("Waiting on clients")


async def test_an_empty_book_says_so_rather_than_printing_a_header(
    session,
):
    assert "Nothing outstanding" in handlers.book_text([], utcnow())


async def test_an_overdue_deal_is_marked_and_counted(
    session, acme_support, support_ops, operator
):
    _, order = await _deal(session, acme_support, operator)
    await _age(session, order, days=8)

    text = handlers.book_text(await fx.outstanding_book(session), utcnow())

    assert handlers.MARK_OVERDUE in text
    assert "1 overdue" in text


async def test_a_line_names_who_to_chase(
    session, acme_support, support_ops, operator
):
    """Rather than making the reader decode the reference.

    The point of the book is that it is read quickly, under pressure, while
    somebody is also being chased by a client.
    """
    _, order = await _deal(session, acme_support, operator)
    order.supplier_code = "SPEX"
    order.status = FxOrderStatus.AWAITING_SETTLEMENT   # waiting on the supplier
    await session.flush()

    entry = (await fx.outstanding_book(session))[0]
    assert "SPEX" in handlers.book_line(entry, utcnow())


# --------------------------------------------------------------------------
# The margin
# --------------------------------------------------------------------------

async def test_the_book_never_carries_a_rate(
    session, acme_support, support_ops, operator
):
    """Both rates exist on an order and the difference between them is what
    NexterPay make. The book is about time, not money - so a rate on one of
    these lines is one forward away from being a leak, and there is no reason
    for it to be there in the first place."""
    _, order = await _deal(session, acme_support, operator)
    order.supplier_rate = Decimal("605.5")
    order.client_rate = Decimal("611.25")
    await session.flush()

    text = handlers.book_text(await fx.outstanding_book(session), utcnow())

    assert "605.5" not in text
    assert "611.25" not in text


def test_the_book_is_never_written_to_a_counterparty() -> None:
    """Structural, because behaviour cannot catch this one.

    Every line holds `display_reference`, which carries the client's code and
    the supplier's together. That is right for the Operations Group and
    catastrophic in either counterparty's group - a supplier who can see the
    client code learns who they are quoting for.

    So `book_text` must only ever reach `message.reply`, which answers where
    the command was typed, and the command is refused outside the Operations
    Group. If it is ever passed to something that takes a destination, this
    fails - which is the point, because by then the destination would be an
    argument and the leak would be a typo away.
    """
    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")

    senders = ("send_message", "send_client_reply", "_announce", "answer(")
    for line in source.splitlines():
        if "book_text(" not in line:
            continue
        assert not any(sender in line for sender in senders), (
            f"book_text is being passed to something that writes outward: "
            f"{line.strip()}"
        )


def test_the_command_is_refused_outside_the_operations_group() -> None:
    """The other half of the guard above, and the one that actually runs in
    production. Checked on the source rather than by driving aiogram, which is
    the same arrangement the rest of this project's handler guards use."""
    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")

    body = source[source.index("async def outstanding_book(message"):]
    body = body[: body.index("async def start_deal")]

    assert "staff_context(" in body
    assert "refusal_reason(" in body
