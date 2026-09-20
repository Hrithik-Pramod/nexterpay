"""Two ways a request finishes, and they are not the same one.

NexterPay asked on 20 September how the end of a request works, and then
settled it in their own words:

    Keep it as it is.
    Completed = internally done, client sees Resolved, no message sent.
    Closed = formally closed, client notified, archive begins.

That is a specification, so it is written down here as one. Nothing in this
file changes behaviour — every assertion passed the day it was written. They
exist because the definition is now agreed, and the half most likely to be
broken by accident is the quiet half: somebody adds a notification to a status
change, which is an entirely reasonable thing to do, and Completed starts
messaging clients who were never meant to hear from it.

The distinction is genuinely useful and worth keeping straight. Completed says
"the work is done"; Closed says "this conversation is over". A desk that marks
work Completed through the afternoon and Closes at the end of it is using the
platform exactly as intended.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.bot.registry import register_archive_chat
from app.domain.enums import Department, WorkItemStatus
from app.domain.work_items import Actor
from app.services import archive, relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def support_archive(session, support_ops):
    return await register_archive_chat(
        session,
        telegram_chat_id=-1009000000055,
        department=Department.SUPPORT,
        title="NPArchive - Support",
    )


async def _open(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="the 14:02 payment never arrived", raised_by_name="Haze",
    )


# --------------------------------------------------------------------------
# Completed — "internally done, client sees Resolved, no message sent"
# --------------------------------------------------------------------------

async def test_completing_sends_the_client_nothing(
    session, acme_support, support_ops, operator, gw
):
    """The quiet half, and the one worth guarding.

    Adding a notification to status changes is a reasonable-looking thing for
    somebody to do. It would turn Completed into a message to a client who
    was never meant to hear from it, and nothing else in the suite would
    notice.
    """
    item = await _open(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.change_status(
        session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator)
    )

    assert item.status is WorkItemStatus.COMPLETED
    assert len(gw.messages_to(CLIENT_CHAT)) == before, (
        "marking a request Completed sent something to the client"
    )


def test_a_completed_request_reads_as_resolved_to_the_client() -> None:
    """Which is why no message is needed. Their list already says it."""
    assert WorkItemStatus.COMPLETED.client_label == "Resolved"


def test_completed_and_closed_read_the_same_to_a_client() -> None:
    """Deliberate. The difference between them is ours, not theirs — a client
    does not need to know whether we have finished the paperwork."""
    assert (
        WorkItemStatus.COMPLETED.client_label
        == WorkItemStatus.CLOSED.client_label
        == "Resolved"
    )


def test_they_are_still_different_internally() -> None:
    """Same word to the client, different states to the desk. The topic list
    shows ✅ against one and 🏁 against the other."""
    assert WorkItemStatus.COMPLETED is not WorkItemStatus.CLOSED
    assert WorkItemStatus.COMPLETED.label == "Completed"
    assert WorkItemStatus.CLOSED.label == "Closed"


# --------------------------------------------------------------------------
# Closed — "formally closed, client notified, archive begins"
# --------------------------------------------------------------------------

async def test_closing_notifies_the_client(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    assert item.status is WorkItemStatus.CLOSED
    assert "is now resolved" in gw.messages_to(CLIENT_CHAT)[-1]


async def test_closing_starts_the_archive_clock(
    session, acme_support, support_ops, operator, gw
):
    """"Archive begins" - and it begins at the moment of closing, which is
    what `closed_at` is for. Twenty-four hours from there."""
    item = await _open(session, gw, acme_support)
    assert item.closed_at is None

    await relay.close(session, gw, item, Actor.of(operator))
    assert item.closed_at is not None


async def test_a_completed_request_is_never_archived(
    session, acme_support, support_ops, operator, gw, support_archive
):
    """The consequence of the distinction, and the reason it is not cosmetic.

    Completed work stays in the live topic list indefinitely. Only Closing
    moves it. A desk that used Completed as its ending would find the list
    growing for ever and nothing explaining why.
    """
    from datetime import timedelta

    from app.db.base import utcnow

    item = await _open(session, gw, acme_support)
    await relay.change_status(
        session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator)
    )
    # Old enough that only the status is keeping it out of the archive.
    item.closed_at = utcnow() - timedelta(hours=48)
    await session.flush()

    assert await archive.due_for_archive(session) == []


async def test_closing_after_completing_works_normally(
    session, acme_support, support_ops, operator, gw
):
    """The ordinary path NexterPay described: done in the afternoon, closed at
    the end of it."""
    item = await _open(session, gw, acme_support)
    await relay.change_status(
        session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator)
    )
    await relay.close(session, gw, item, Actor.of(operator))

    assert item.status is WorkItemStatus.CLOSED
    assert "is now resolved" in gw.messages_to(CLIENT_CHAT)[-1]


async def test_closing_without_completing_works_too(
    session, acme_support, support_ops, operator, gw
):
    """Completed is optional and always was. Neither requires the other."""
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))
    assert item.status is WorkItemStatus.CLOSED
