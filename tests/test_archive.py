"""Moving finished work out of the desk's topic list.

NexterPay, 9 September, settled on the 14th: a second forum group per desk, the
closed ticket copied into it 24 hours after closing, the archive topic made
read-only, and the original removed.

Most of what could go wrong here is silent, so most of these tests are about
the order things happen in rather than about whether they happen.

    * The original topic is deleted only after the copy exists and
      `archived_at` is written. The other order loses the only record on a bad
      day, and nobody finds out until somebody goes looking months later.

    * `archived_at` is the idempotency key. The sweep runs on a timer, and a
      timer that dies halfway will run again — so a ticket must be safe to
      re-archive, and one that failed must not be quietly skipped forever.

    * Forwarded, never copied. NexterPay chose that on the 12th. A copy is the
      bot saying somebody else's words; a forward keeps the author. An archive
      that attributes every line to the bot answers "what was said" and not
      "who said it".
"""

from __future__ import annotations

from datetime import timedelta
from itertools import count

import pytest
import pytest_asyncio

from app.bot.registry import register_archive_chat
from app.db.base import utcnow
from app.domain.enums import Department, WorkItemStatus
from app.domain.work_items import Actor
from app.services import archive, relay
from app.services.gateway import FakeGateway

ARCHIVE_CHAT = -1009000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def support_archive(session, support_ops):
    return await register_archive_chat(
        session,
        telegram_chat_id=ARCHIVE_CHAT,
        department=Department.SUPPORT,
        title="Support — Closed",
    )


# Client message ids, unique per test run.
#
# The pair (chat, message id) is unique in the messages table, so a helper that
# handed out a fixed id would collide the moment a test raised two requests in
# the same group - which the starvation test below does ten times.
_client_message_ids = count(5001)


async def _closed(session, gw, chat, operator, *, hours_ago: float):
    """A request raised, then closed, with the clock wound back."""
    item = await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="the 14:02 payment never arrived", raised_by_name="Haze",
        original_telegram_message_id=next(_client_message_ids),
    )
    await relay.close(session, gw, item, Actor.of(operator))
    item.closed_at = utcnow() - timedelta(hours=hours_ago)
    await session.flush()
    return item


# --------------------------------------------------------------------------
# What is due
# --------------------------------------------------------------------------

async def test_a_request_closed_yesterday_is_due(
    session, acme_support, support_ops, operator, gw, support_archive
):
    item = await _closed(session, gw, acme_support, operator, hours_ago=25)
    assert [d.id for d in await archive.due_for_archive(session)] == [item.id]


async def test_a_request_closed_an_hour_ago_is_left_alone(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """The 24 hours are for the desk, not the archive.

    Somebody closes a ticket, the client replies an hour later saying it is not
    fixed, and reopening a topic that still exists is a very different
    afternoon from reconstructing one that does not.

    `support_archive` is requested so this fails for the reason it names. The
    desk now has to be configured for anything to be due at all, so without
    the fixture an empty list would prove the 24 hours were respected when it
    actually only proved the desk had nowhere to archive to.
    """
    await _closed(session, gw, acme_support, operator, hours_ago=1)
    assert await archive.due_for_archive(session) == []


async def test_an_open_request_is_never_due(
    session, acme_support, support_ops, gw, support_archive
):
    await relay.open_request(
        session, gw, source_chat=acme_support, subject="Still open",
        body="ongoing", raised_by_name="Haze",
    )
    assert await archive.due_for_archive(session) == []


# --------------------------------------------------------------------------
# What an unconfigured desk must not do to a configured one
#
# Found in production on 18 September, not by this suite. The archive had been
# working for a week and had stopped moving anything at all, silently, while
# reporting success-shaped INFO lines every fifteen minutes.
# --------------------------------------------------------------------------

async def test_a_desk_with_no_archive_group_is_not_due(
    session, acme_compliance, operator, gw
):
    """Nowhere to put it is not the same as due but failing.

    This is the whole fix in one line. The old code called this ticket due,
    handed it to `archive_one`, got False back, logged, and left
    `archived_at` null — so it was due again fifteen minutes later, and for
    ever.
    """
    await _closed(session, gw, acme_compliance, operator, hours_ago=30)
    assert await archive.due_for_archive(session) == []


async def test_an_unconfigured_backlog_cannot_starve_a_configured_desk(
    session, acme_support, support_ops, acme_compliance, operator, gw,
    support_archive,
):
    """The production fault, reproduced.

    Compliance has no archive group and a backlog older than anything on
    Support. The batch is filled oldest-first, so under the old code those
    tickets took every slot on every sweep and Support's work — which had
    somewhere to go — was never reached. Nothing errored. The logs looked
    busy. The archive had simply stopped.

    `BATCH` items are used deliberately: one stale ticket would not have shown
    this, because the batch had room for both. It needed a backlog exactly as
    large as the batch, which is the shape a real desk reaches on its own.
    """
    for _ in range(archive.BATCH):
        await _closed(session, gw, acme_compliance, operator, hours_ago=200)

    wants_archiving = await _closed(
        session, gw, acme_support, operator, hours_ago=30
    )

    due = await archive.due_for_archive(session)
    assert [d.id for d in due] == [wants_archiving.id]

    assert await archive.sweep(session, gw) == 1
    await session.refresh(wants_archiving)
    assert wants_archiving.archived_at is not None


async def test_configuring_a_desk_lets_its_backlog_drain(
    session, acme_compliance, operator, gw, support_ops
):
    """And the tickets that were stranded are not lost.

    They were never modified while there was nowhere to put them, so
    registering the group is the whole of the remedy — nothing has to be
    replayed or repaired by hand. Which is the one thing the old behaviour
    did get right.
    """
    await _closed(session, gw, acme_compliance, operator, hours_ago=30)
    assert await archive.due_for_archive(session) == []

    await register_archive_chat(
        session,
        telegram_chat_id=-1009000000002,
        department=Department.COMPLIANCE,
        title="Compliance — Closed",
    )
    assert len(await archive.due_for_archive(session)) == 1


async def test_a_platform_with_no_archives_at_all_sweeps_nothing(
    session, acme_support, support_ops, operator, gw
):
    """The state every deployment starts in. It must cost one query, not a
    batch of work that cannot succeed."""
    await _closed(session, gw, acme_support, operator, hours_ago=30)
    assert await archive.archiving_departments(session) == []
    assert await archive.due_for_archive(session) == []
    assert await archive.sweep(session, gw) == 0


async def test_an_archived_request_is_not_due_again(
    session, acme_support, support_ops, operator, gw, support_archive
):
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    assert await archive.due_for_archive(session) == []


# --------------------------------------------------------------------------
# Moving one
# --------------------------------------------------------------------------

async def test_the_ticket_is_copied_then_the_original_removed(
    session, acme_support, support_ops, operator, gw, support_archive
):
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    original_topic = item.topic_id

    assert await archive.archive_one(session, gw, item) is True

    # A topic in the archive, carrying the reference.
    assert item.archive_topic_id is not None
    assert item.archived_at is not None
    created = [c for c in gw.calls if c.method == "create_topic"]
    assert any(item.display_reference in c.payload["name"] for c in created)

    # Read-only, as asked.
    assert (ARCHIVE_CHAT, item.archive_topic_id) in gw.closed_topics

    # And only then is the original gone.
    assert (support_ops.telegram_chat_id, original_topic) in gw.deleted_topics


async def test_the_conversation_is_forwarded_not_retyped(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """The decision this whole module is shaped around."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)

    assert gw.forwarded, "nothing was forwarded into the archive"
    assert all(to == ARCHIVE_CHAT for to, _, _, _ in gw.forwarded)
    assert all(thread == item.archive_topic_id for *_, thread in gw.forwarded)


async def test_the_archive_contains_what_the_client_actually_said(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """The other half of the conversation.

    Found by opening the real archive on 18 September rather than by this
    suite, which is the uncomfortable part: every assertion above passed
    while every archived ticket held only NexterPay's outbound messages.
    The client's opening words were on the work item as a column and were
    never a `Message`, so there was nothing for the sweep to forward.

    Checked as "forwarded from the client's own group" rather than as a count,
    because the count was never the problem - four messages were being
    forwarded quite happily. What was missing was whose they were.
    """
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)

    from_client_group = [
        message_id
        for _, from_chat, message_id, _ in gw.forwarded
        if from_chat == acme_support.telegram_chat_id
    ]
    assert from_client_group, (
        "the archive holds only our side of the conversation - the client's "
        "opening message was never forwarded"
    )


async def test_the_client_s_opening_words_come_first(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """A conversation that opens with our acknowledgement reads as though we
    started it. The archive should open the way the real exchange did."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)

    first_from = gw.forwarded[0][1]
    assert first_from == acme_support.telegram_chat_id


async def test_the_summary_carries_what_they_asked_for(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """Owner, status history and resolution, in one place at the top."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)

    posted = [
        c for c in gw.calls
        if c.method == "send_message" and c.chat_id == ARCHIVE_CHAT
    ]
    assert posted, "no summary was posted"
    summary = posted[0].payload["text"]
    assert item.display_reference in summary
    assert "Acme Payments" in summary
    assert "Settlement missing" in summary
    assert "History" in summary


async def test_a_desk_with_no_archive_is_left_exactly_as_it_was(
    session, acme_support, support_ops, operator, gw
):
    """A configuration state, not a failure. Housekeeping must never be the
    reason a ticket is disturbed."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    topic = item.topic_id

    assert await archive.archive_one(session, gw, item) is False

    assert item.archived_at is None
    assert item.archive_topic_id is None
    assert item.topic_id == topic
    assert gw.deleted_topics == []
    assert gw.forwarded == []


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

async def test_the_sweep_moves_what_is_due(
    session, acme_support, support_ops, operator, gw, support_archive
):
    await _closed(session, gw, acme_support, operator, hours_ago=30)
    assert await archive.sweep(session, gw) == 1


async def test_running_the_sweep_twice_archives_once(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """A timer that died halfway will run again. `archived_at` is what makes
    that safe, and this is the test that proves it."""
    await _closed(session, gw, acme_support, operator, hours_ago=30)

    assert await archive.sweep(session, gw) == 1
    before = len(gw.forwarded)

    assert await archive.sweep(session, gw) == 0
    assert len(gw.forwarded) == before, "the ticket was archived a second time"


async def test_one_bad_ticket_does_not_stop_the_rest(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """One desk with the wrong permissions must not hold up every other desk's
    housekeeping — and the ticket that failed must come back, not vanish."""
    first = await _closed(session, gw, acme_support, operator, hours_ago=31)
    await _closed(session, gw, acme_support, operator, hours_ago=30)

    gw.fail_next = RuntimeError("Bad Request: not enough rights")
    moved = await archive.sweep(session, gw)

    assert moved == 1, "the second ticket should still have moved"
    assert first.archived_at is None, "the failed one was marked done anyway"
    assert first.id in [d.id for d in await archive.due_for_archive(session)]


# --------------------------------------------------------------------------
# Reopening something the archive has already taken
#
# NexterPay, 9 September: "If reopened, a new active topic would be created and
# linked back to the archived ticket."
#
# This was missed when the archive was built, and missing it left reopen worse
# than incomplete: the original topic had been deleted but the work item still
# held its id, so reopening wrote into a thread that no longer existed.
# --------------------------------------------------------------------------

async def test_archiving_forgets_the_topic_it_deleted(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """The id has to go with the topic.

    Every path that posts into a topic checks for None; not one of them checks
    whether the topic still exists, because until the archive was built it
    always did.
    """
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    assert item.topic_id is None


async def test_reopening_an_archived_request_gets_a_new_topic(
    session, acme_support, support_ops, operator, manager, gw, support_archive
):
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    archived_topic = item.archive_topic_id

    await relay.reopen(session, gw, item, Actor.of(manager))

    assert item.topic_id is not None, "reopened with nowhere to work"
    assert item.topic_id != archived_topic
    assert (support_ops.telegram_chat_id, item.topic_id) not in gw.reopened_topics, (
        "it tried to reopen a topic instead of creating one"
    )


async def test_the_archived_copy_is_left_alone(
    session, acme_support, support_ops, operator, manager, gw, support_archive
):
    """It is the record of how the ticket finished the first time. Deleting it
    to make the reopened one the single version destroys the thing the archive
    exists for."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    archived_topic = item.archive_topic_id

    await relay.reopen(session, gw, item, Actor.of(manager))

    assert (ARCHIVE_CHAT, archived_topic) not in gw.deleted_topics


async def test_the_new_topic_links_back(
    session, acme_support, support_ops, operator, manager, gw, support_archive
):
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    await relay.reopen(session, gw, item, Actor.of(manager))

    posted = " ".join(
        c.payload.get("text", "") for c in gw.calls
        if c.method == "send_message" and c.chat_id == support_ops.telegram_chat_id
    )
    assert "archived" in posted.lower()
    assert "https://t.me/c/" in posted, "no link back to the archived copy"


async def test_closing_it_again_archives_it_again(
    session, acme_support, support_ops, operator, manager, gw, support_archive
):
    """`archived_at` is cleared on reopen, so a second closure is not skipped
    by a sweep that thinks the work is already done."""
    item = await _closed(session, gw, acme_support, operator, hours_ago=30)
    await archive.archive_one(session, gw, item)
    await relay.reopen(session, gw, item, Actor.of(manager))

    assert item.archived_at is None
    assert item.archive_topic_id is None

    await relay.close(session, gw, item, Actor.of(operator))
    item.closed_at = utcnow() - timedelta(hours=30)
    await session.flush()

    assert [d.id for d in await archive.due_for_archive(session)] == [item.id]


def test_a_link_is_only_built_for_a_real_supergroup() -> None:
    """A dead link in an Operations topic is worse than a sentence saying where
    to look, so anything that is not a -100 supergroup gets no link at all."""
    assert relay.archive_link(-1009000000001, 42) == (
        "https://t.me/c/9000000001/42"
    )
    assert relay.archive_link(12345, 42) is None


# --------------------------------------------------------------------------
# One archive per desk
# --------------------------------------------------------------------------

async def test_a_second_archive_for_the_same_desk_is_refused(
    session, support_ops, support_archive
):
    """Enforced in code rather than by a unique index, because a partial index
    on a newly added enum value cannot be created in the migration that adds
    the value — the 12 September failure in a different hat.

    Two archives would split a desk's history in half with nothing to make the
    mistake visible, since nothing is ever raised in an archive.
    """
    with pytest.raises(ValueError) as caught:
        await register_archive_chat(
            session,
            telegram_chat_id=-1009000000002,
            department=Department.SUPPORT,
            title="Support — Closed (second)",
        )
    assert "Support — Closed" in str(caught.value)


async def test_re_registering_the_same_group_is_fine(
    session, support_ops, support_archive
):
    """Somebody running the command twice in the right group has made no
    mistake, and refusing them would look like a fault."""
    again = await register_archive_chat(
        session,
        telegram_chat_id=ARCHIVE_CHAT,
        department=Department.SUPPORT,
        title="Support — Closed",
    )
    assert again.id == support_archive.id


async def test_each_desk_gets_its_own(session, support_ops):
    """Support and Finance archiving to the same group would be worse than no
    archive at all."""
    await register_archive_chat(
        session, telegram_chat_id=-1009000000003,
        department=Department.SUPPORT, title="Support — Closed",
    )
    finance = await register_archive_chat(
        session, telegram_chat_id=-1009000000004,
        department=Department.FINANCE, title="Finance — Closed",
    )
    assert finance.department is Department.FINANCE
    found = await archive.archive_chat_for(session, Department.FINANCE)
    assert found is not None and found.telegram_chat_id == -1009000000004


# --------------------------------------------------------------------------
# The delay itself
# --------------------------------------------------------------------------

def test_the_delay_is_the_one_they_settled_on() -> None:
    """24 hours. NexterPay said 24 on the 12th, 3 days in a later note, and
    then "Lets do 24hours, if its closed it can go" on the 14th."""
    assert archive.ARCHIVE_AFTER == timedelta(hours=24)


def test_closed_is_the_only_status_that_qualifies() -> None:
    """Completed is not closed. NexterPay were asked directly and were firm:
    work finished but not signed off is still live work."""
    import ast
    import inspect

    source = ast.unparse(ast.parse(inspect.getsource(archive.due_for_archive)))
    assert "WorkItemStatus.CLOSED" in source
    assert "COMPLETED" not in source
    assert WorkItemStatus.COMPLETED is not WorkItemStatus.CLOSED
