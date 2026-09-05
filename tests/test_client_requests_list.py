"""The client's view of their own open requests.

NexterPay's decisions: the whole group's requests rather than one person's,
open ones only, and clients see a coarser set of statuses than staff track.
"""

from __future__ import annotations

import pytest

from app.bot import keyboards as kb
from app.bot.handlers.client import LIST_LIMIT, _request_list
from app.db.models import Client
from app.domain.enums import WorkItemStatus
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _raise(session, gw, chat, subject):
    return await relay.open_request(
        session, gw, source_chat=chat, subject=subject,
        body=f"about {subject}", raised_by_name="Tom Baker",
    )


async def test_only_open_requests_are_listed(
    session, acme_support, support_ops, operator, gw
):
    first = await _raise(session, gw, acme_support, "Settlement")
    second = await _raise(session, gw, acme_support, "Onboarding")
    await relay.close(session, gw, second, Actor.of(operator))

    listed = await relay.open_requests_for(session, acme_support)
    subjects = [i.subject for i in listed]

    assert "Settlement" in subjects
    assert "Onboarding" not in subjects, "closed requests must not appear"
    assert listed[0].id == first.id


async def test_clients_see_the_simplified_status_wording() -> None:
    """Internal process wording must not reach a customer.

    "Escalated" and "Waiting for Third Party" describe NexterPay's process,
    not the client's situation.
    """
    assert WorkItemStatus.OPEN.client_label == "Received"
    assert WorkItemStatus.WAITING_CLIENT.client_label == "Waiting on you"
    assert WorkItemStatus.ESCALATED.client_label == "In progress"
    assert WorkItemStatus.WAITING_THIRD_PARTY.client_label == "In progress"
    assert WorkItemStatus.COMPLETED.client_label == "Resolved"

    internal_only = {"Escalated", "Waiting for Third Party", "Waiting for Internal Team"}
    shown = {s.client_label for s in WorkItemStatus}
    assert not (shown & internal_only), f"internal wording leaked: {shown & internal_only}"


async def test_the_anchor_lets_a_reply_reach_the_right_request(
    session, acme_support, support_ops, gw
):
    """Tapping a request posts something the client can reply to.

    It reuses the routing that already works rather than inventing a second
    mechanism, so a reply to the anchor resolves to that work item.
    """
    from app.bot.routing import IncomingMessage, ReplyToAcknowledgementStrategy

    item = await _raise(session, gw, acme_support, "Settlement")
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.post_anchor(session, gw, item)
    assert len(gw.messages_to(CLIENT_CHAT)) == before + 1

    # Read the anchor back from the database rather than the fake, because the
    # recorded row is what the routing strategy actually looks up.
    from sqlalchemy import select

    from app.db.models import Message

    rows = await session.execute(
        select(Message)
        .where(Message.work_item_id == item.id, Message.telegram_chat_id == CLIENT_CHAT)
        .order_by(Message.id.desc())
    )
    anchor_id = rows.scalars().first().telegram_message_id
    resolved = await ReplyToAcknowledgementStrategy().resolve(
        session, acme_support,
        IncomingMessage(
            telegram_chat_id=CLIENT_CHAT,
            telegram_message_id=999,
            sender_name="Tom Baker",
            text="any update?",
            reply_to_message_id=anchor_id,
        ),
    )
    assert resolved is not None and resolved.id == item.id


async def test_the_anchor_never_carries_the_supplier_code(
    session, acme_support, support_ops, operator, gw
):
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    item = await _raise(session, gw, acme_support, "Settlement")
    supplier = Client(name="Supplier Pexi", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    await relay.post_anchor(session, gw, item)
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)
    assert "ACME-" in gw.all_text_to(CLIENT_CHAT)


# --------------------------------------------------------------------------
# Four weeks of history
# --------------------------------------------------------------------------

async def test_recently_closed_requests_appear_but_old_ones_do_not(
    session, acme_support, support_ops, manager, gw
):
    """NexterPay chose four weeks.

    Long enough to answer "what happened to the thing from a fortnight ago",
    short enough that a group running for a year does not reply with a wall.
    """
    from datetime import timedelta

    from app.db.base import utcnow
    from app.domain import work_items as wi

    live = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Still going",
        body="Open.", raised_by_name="Tom Baker",
    )
    recent = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Finished last week",
        body="Closed recently.", raised_by_name="Tom Baker",
    )
    ancient = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Finished in the spring",
        body="Closed long ago.", raised_by_name="Tom Baker",
    )

    await relay.close(session, gw, recent, Actor.of(manager))
    await relay.close(session, gw, ancient, Actor.of(manager))
    # Push one of them outside the window.
    ancient.closed_at = utcnow() - wi.CLIENT_HISTORY - timedelta(days=1)
    await session.flush()

    shown = {i.id for i in await relay.open_requests_for(
        session, acme_support, recent_closed=True
    )}
    assert live.id in shown
    assert recent.id in shown
    assert ancient.id not in shown, "a request closed months ago is not recent news"

    # And the default is unchanged, so nothing else that calls this shifted.
    only_open = {i.id for i in await relay.open_requests_for(session, acme_support)}
    assert only_open == {live.id}


async def test_the_window_is_measured_from_when_it_closed(
    session, acme_support, support_ops, manager, gw
):
    """A request that ran for months and closed yesterday is recent news.

    Measuring from when it was raised would drop exactly the long-running
    ones a client is most likely to be asking about.
    """
    from datetime import timedelta

    from app.db.base import utcnow
    from app.domain import work_items as wi

    old_but_recent = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Long runner",
        body="Raised in the spring.", raised_by_name="Tom Baker",
    )
    old_but_recent.created_at = utcnow() - wi.CLIENT_HISTORY - timedelta(days=90)
    await session.flush()
    await relay.close(session, gw, old_but_recent, Actor.of(manager))

    shown = {i.id for i in await relay.open_requests_for(
        session, acme_support, recent_closed=True
    )}
    assert old_but_recent.id in shown


async def test_the_front_door_offers_looking_as_well_as_raising() -> None:
    """Gavin's point, and a fair one.

    Somebody sending /np is as likely to be chasing something they already
    raised as starting something new. Offering only "Raise Request" makes
    checking require knowing a second command exists - and quietly encourages
    a duplicate, which is then something NexterPay has to close by hand.
    """
    from app.bot import keyboards as kb

    for department in ("support", "business"):
        labels = [
            b.text
            for row in kb.raise_request_prompt(department).inline_keyboard
            for b in row
        ]
        assert any("request" in t.lower() or "enquiry" in t.lower() for t in labels)
        assert "My requests" in labels, f"{department} cannot look without raising"

    # Business still gets its own wording for the raising half.
    business = kb.raise_request_prompt("business").inline_keyboard[0][0]
    assert business.text == "Commercial Enquiry"


# --------------------------------------------------------------------------
# How much of it is shown
#
# Found in live testing on 5 September, with twenty-odd requests in the Acme
# group: there was no limit at all. One text line and one button per request,
# in one message, growing forever.
#
# Telegram rejects a message over 4096 characters outright - it does not
# truncate. So a client with enough history sends /nptickets and gets nothing
# back: no list, no error, and no way to tell that from the bot being down.
# The same silent failure this project has now paid for three times.
# --------------------------------------------------------------------------

class _Item:
    """Enough of a WorkItem to render a line and a button."""

    def __init__(self, n: int, status: WorkItemStatus, subject: str = "Something") -> None:
        self.id = n
        self.reference = n
        self.client_reference = f"ACME-{n}"
        self.subject = subject
        self.status = status


def _many(count: int, status=WorkItemStatus.OPEN) -> list[_Item]:
    return [_Item(1000 + n, status) for n in range(count)]


def test_a_short_list_is_shown_whole() -> None:
    text, markup = _request_list(_many(3))

    assert text.count("ACME-") == 3
    assert len(markup.inline_keyboard) == 3
    assert "not shown" not in text


def test_a_long_list_is_capped() -> None:
    text, markup = _request_list(_many(40))

    assert text.count("ACME-") == LIST_LIMIT
    assert len(markup.inline_keyboard) == LIST_LIMIT
    assert "30 older ones not shown" in text


def test_the_message_stays_under_telegrams_limit() -> None:
    """The actual failure being prevented, measured rather than assumed.

    Long subjects, because a client describing a payment problem writes a
    sentence, not a word - and the cap has to hold for the worst realistic
    case rather than the tidy one.
    """
    wordy = [
        _Item(1000 + n, WorkItemStatus.OPEN,
              "Card payments have been failing intermittently since this "
              "morning for our EUR corridor, please investigate urgently")
        for n in range(200)
    ]
    text, _ = _request_list(wordy)

    assert len(text) < 4096, f"{len(text)} characters - Telegram would reject this"


def test_an_open_request_is_never_hidden_behind_resolved_ones() -> None:
    """Trimming by recency alone would push a live request off the end.

    That is exactly backwards: the open ones are why anybody asks. A client
    chasing something would be shown a screen of finished work and conclude
    their request had vanished.
    """
    resolved = [_Item(2000 + n, WorkItemStatus.CLOSED) for n in range(30)]
    live = [_Item(3000 + n, WorkItemStatus.OPEN) for n in range(4)]

    text, _ = _request_list(resolved + live)

    for item in live:
        assert item.client_reference in text, (
            f"{item.client_reference} is open and was hidden"
        )


def test_the_cap_is_a_screenful() -> None:
    """Ten because that is what fits on a phone, which is the real limit.
    A number chosen to satisfy Telegram alone would be far higher and far
    less usable."""
    assert 5 <= LIST_LIMIT <= 15


def test_it_still_says_what_the_list_contains() -> None:
    """The four-week window is not obvious and has to be stated, or a client
    reads a resolved item as still open."""
    text, _ = _request_list(_many(2))
    assert "four weeks" in text
    assert "Tap one" in text


def test_both_ways_in_offer_the_same_name() -> None:
    """The command, the menu button and the acknowledgement button all reach
    one list. It carried two different names, and one of them - "My open
    requests" - was wrong, because the list is not only open ones."""
    ack = [b.text for row in kb.acknowledgement_actions().inline_keyboard for b in row]
    menu = [b.text for row in kb.raise_request_prompt("support").inline_keyboard for b in row]

    assert "My requests" in ack
    assert "My requests" in menu
    assert not any("open requests" in t for t in ack + menu)


def test_the_buttons_and_the_lines_agree() -> None:
    """A button for a request that is not in the text, or the reverse, is a
    list that lies about itself."""
    text, markup = _request_list(_many(25))
    on_buttons = [row[0].text.split(" ·")[0] for row in markup.inline_keyboard]

    for reference in on_buttons:
        assert reference in text, f"{reference} has a button but no line"
    assert len(on_buttons) == text.count("ACME-")
