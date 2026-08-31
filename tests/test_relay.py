"""The relay, end to end, against a fake Telegram.

The most important tests in this file are the leak tests. Everything else can
be fixed after go-live; an internal note reaching a client cannot.
"""

from __future__ import annotations

import pytest

from app.domain import work_items as wi
from app.domain.enums import MessageDirection, Priority, WorkItemStatus
from app.domain.history import load_events, render_history
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway
from app.services.relay import IncomingAttachment

# Must match the fixtures in conftest.py
CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _open(session, gw, chat, subject="Settlement missing", attachments=None):
    return await relay.open_request(
        session, gw,
        source_chat=chat,
        subject=subject,
        body="We have not received settlement for 3 March.",
        raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
        attachments=attachments,
    )


# --- opening a request -----------------------------------------------------

async def test_opening_a_request_creates_topic_and_acknowledges(
    session, acme_support, support_ops, gw
):
    item = await _open(session, gw, acme_support)

    assert item.topic_id is not None
    assert gw.topics[OPS_CHAT] == [item.topic_id]

    ack = gw.messages_to(CLIENT_CHAT)
    assert len(ack) == 1
    assert item.display_reference in ack[0]
    assert "reply to this message" in ack[0].lower()

    topic_text = gw.all_text_to(OPS_CHAT)
    assert item.display_reference in topic_text
    assert "Acme Payments" in topic_text
    assert "Tom Baker" in topic_text


async def test_acknowledgement_is_recorded_as_the_routing_anchor(
    session, acme_support, support_ops, gw
):
    """If the ack is not stored, a client reply to it cannot be matched."""
    from sqlalchemy import select

    from app.db.models import Message

    item = await _open(session, gw, acme_support)
    result = await session.execute(
        select(Message).where(
            Message.work_item_id == item.id,
            Message.direction == MessageDirection.OUTBOUND,
        )
    )
    anchors = list(result.scalars().all())
    assert len(anchors) == 1
    assert anchors[0].telegram_chat_id == CLIENT_CHAT


async def test_client_attachments_relay_by_file_id_without_download(
    session, acme_support, support_ops, gw
):
    att = IncomingAttachment(
        file_id="BQACAgQAAx0", file_unique_id="u1", kind="document",
        file_name="statement.pdf", mime_type="application/pdf",
        file_size=45_000_000,  # far over the 20 MB download ceiling
    )
    item = await _open(session, gw, acme_support, attachments=[att])

    assert gw.files_to(OPS_CHAT) == ["BQACAgQAAx0"]

    from sqlalchemy import select

    from app.db.models import Attachment

    stored = (await session.execute(
        select(Attachment).where(Attachment.work_item_id == item.id)
    )).scalars().all()
    assert len(stored) == 1
    assert stored[0].stored_path is None, "we relay by reference, we do not download"


# --- inbound ---------------------------------------------------------------

async def test_client_reply_reaches_the_topic(session, acme_support, support_ops, gw):
    item = await _open(session, gw, acme_support)

    await relay.relay_client_message(
        session, gw, item,
        text="Here is the confirmation.",
        sender_name="Tom Baker",
        telegram_message_id=301,
    )

    topic_text = gw.all_text_to(OPS_CHAT)
    assert "Here is the confirmation." in topic_text
    assert "Message received from Tom Baker" in topic_text


# --- outbound and the leak tests -------------------------------------------

async def test_staff_reply_reaches_the_client_with_reference(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "We are looking into this now."
    )

    client_messages = gw.messages_to(CLIENT_CHAT)
    assert any("We are looking into this now." in m for m in client_messages)
    assert any(item.display_reference in m for m in client_messages)


async def test_internal_note_never_reaches_the_client(
    session, acme_support, support_ops, operator, gw
):
    """The one that matters."""
    item = await _open(session, gw, acme_support)
    before = list(gw.messages_to(CLIENT_CHAT))

    await relay.add_internal_note(
        session, gw, item, Actor.of(operator),
        "This client is consistently late paying - do not prioritise.",
    )

    after = gw.messages_to(CLIENT_CHAT)
    assert after == before, "internal note produced a message in the client group"
    assert "consistently late" not in gw.all_text_to(CLIENT_CHAT)


async def test_note_text_is_not_echoed_into_the_topic_announcement(
    session, acme_support, support_ops, operator, gw
):
    """The announcement says a note was added; it does not repeat the note."""
    item = await _open(session, gw, acme_support)
    await relay.add_internal_note(
        session, gw, item, Actor.of(operator), "secret internal reasoning"
    )
    assert "Internal note by Sarah Hill" in gw.all_text_to(OPS_CHAT)


async def test_status_and_priority_changes_are_visible_in_the_topic(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await _open(session, gw, acme_support)

    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.change_status(session, gw, item, WorkItemStatus.WAITING_CLIENT, Actor.of(operator))
    await relay.change_priority(session, gw, item, Priority.HIGH, Actor.of(senior))

    topic_text = gw.all_text_to(OPS_CHAT)
    assert "Claimed by Sarah Hill" in topic_text
    assert "Status: Open → In Progress (Sarah Hill)" in topic_text
    assert "Status: In Progress → Waiting for Client (Sarah Hill)" in topic_text
    assert "Priority: Medium → High (James Okoro)" in topic_text

    # And none of it leaked outward.
    assert "Priority" not in gw.all_text_to(CLIENT_CHAT)


async def test_no_op_change_announces_nothing(
    session, acme_support, support_ops, senior, gw
):
    item = await _open(session, gw, acme_support)
    before = len(gw.messages_to(OPS_CHAT))

    await relay.change_priority(session, gw, item, Priority.MEDIUM, Actor.of(senior))

    assert len(gw.messages_to(OPS_CHAT)) == before


# --- closing ---------------------------------------------------------------

async def test_close_archives_topic_and_tells_the_client(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    assert item.status is WorkItemStatus.CLOSED
    assert (OPS_CHAT, item.topic_id) in gw.closed_topics

    # NexterPay asked for the original request to be repeated back, because a
    # bare "this is closed" arriving days later means nothing to the reader.
    closure = [m for m in gw.messages_to(CLIENT_CHAT) if "is now resolved" in m]
    assert closure, "the client was not told"
    assert "What you raised on" in closure[0]
    assert "settlement" in closure[0].lower()


async def test_close_can_be_silent(session, acme_support, support_ops, operator, gw):
    item = await _open(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.close(session, gw, item, Actor.of(operator), notify_client=False)

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert (OPS_CHAT, item.topic_id) in gw.closed_topics


# --- the whole trail -------------------------------------------------------

async def test_full_journey_reads_correctly_in_the_history(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.relay_client_message(
        session, gw, item, text="Any update?", sender_name="Tom Baker",
        telegram_message_id=310,
    )
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Investigating with our bank now."
    )
    await relay.change_status(
        session, gw, item, WorkItemStatus.WAITING_THIRD_PARTY, Actor.of(operator)
    )
    await relay.add_internal_note(
        session, gw, item, Actor.of(senior), "Chased the bank at 14:00."
    )
    await relay.close(session, gw, item, Actor.of(operator))

    lines = render_history(await load_events(session, item))
    joined = "\n".join(lines)

    for expected in [
        "Work Item created by Tom Baker",
        "Topic opened",
        "Claimed by Sarah Hill",
        "Message received from Tom Baker",
        "Reply sent to client by Sarah Hill",
        "Waiting for Third Party",
        "Internal note by James Okoro",
        "Closed by Sarah Hill",
    ]:
        assert expected in joined, f"missing from history: {expected}"

    # Nothing internal escaped at any point in that journey.
    assert "Chased the bank" not in gw.all_text_to(CLIENT_CHAT)


async def test_unclaimed_item_can_still_be_replied_to(
    session, acme_support, support_ops, operator, gw
):
    """Ownership is not a precondition for helping a client."""
    item = await _open(session, gw, acme_support)
    await relay.send_client_reply(session, gw, item, Actor.of(operator), "On it.")
    assert any("On it." in m for m in gw.messages_to(CLIENT_CHAT))


async def test_client_cannot_act_as_staff(session, acme_support, support_ops, gw):
    from app.domain.errors import NotAuthorised

    item = await _open(session, gw, acme_support)
    stranger = Actor(name="Tom Baker", telegram_user_id=9001)

    with pytest.raises(NotAuthorised):
        await relay.send_client_reply(session, gw, item, stranger, "hello")
    with pytest.raises(NotAuthorised):
        await wi.claim(session, item, stranger)


# --- PRD 7.5: staff attachments must reach the client ----------------------

async def test_staff_attachment_reaches_the_client(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    proof = IncomingAttachment(
        file_id="STAFFFILE1", file_unique_id="s1", kind="document",
        file_name="settlement-advice.pdf",
    )

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator),
        "please see the settlement advice attached.",
        attachment=proof,
    )

    assert gw.files_to(CLIENT_CHAT) == ["STAFFFILE1"]


async def test_internal_attachment_stays_internal(
    session, acme_support, support_ops, senior, gw
):
    item = await _open(session, gw, acme_support)
    internal = IncomingAttachment(
        file_id="INTERNAL1", file_unique_id="i1", kind="document",
        file_name="bank-escalation-notes.pdf",
    )

    await relay.record_internal_attachment(
        session, gw, item, Actor.of(senior), [internal], note="Notes from the bank call.",
    )

    assert "INTERNAL1" not in gw.files_to(CLIENT_CHAT)
    assert "Notes from the bank call." not in gw.all_text_to(CLIENT_CHAT)


# --- PRD 3.6: the new owner is told ----------------------------------------

async def test_assignment_notifies_the_new_owner(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await _open(session, gw, acme_support)
    await relay.assign(session, gw, item, operator, Actor.of(senior))

    topic_text = gw.all_text_to(OPS_CHAT)
    assert "is now assigned to you" in topic_text
    assert str(operator.telegram_user_id) in topic_text, "should mention them by id"
    assert "assigned to you" not in gw.all_text_to(CLIENT_CHAT)


async def test_claiming_does_not_notify_yourself(
    session, acme_support, support_ops, operator, gw
):
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))
    assert "is now assigned to you" not in gw.all_text_to(OPS_CHAT)


async def test_client_chasing_a_claimed_item_pings_the_owner(
    session, acme_support, support_ops, operator, gw
):
    """NexterPay's decision: the owner is mentioned, not merely written about.

    A message sitting in a topic is easy to miss in a busy group. A real
    tg://user mention produces a notification for the person who claimed it.
    """
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="any update on this?",
        sender_name="Tom Baker",
        telegram_message_id=777,
    )

    topic = gw.all_text_to(OPS_CHAT)
    assert f'tg://user?id={operator.telegram_user_id}' in topic
    assert "any update on this?" in topic

    mention_calls = [
        c for c in gw.calls
        if c.method == "send_message" and "tg://user" in c.payload.get("text", "")
    ]
    assert all(c.payload.get("parse_mode") == "HTML" for c in mention_calls), (
        "a mention sent without parse_mode arrives as literal HTML and pings nobody"
    )


async def test_unowned_item_is_relayed_without_a_mention(
    session, acme_support, support_ops, gw
):
    item = await _open(session, gw, acme_support)

    await relay.relay_client_message(
        session, gw, item,
        text="still waiting",
        sender_name="Tom Baker",
        telegram_message_id=778,
    )

    topic = gw.all_text_to(OPS_CHAT)
    assert "still waiting" in topic
    assert "tg://user" not in topic


async def test_client_text_with_markup_characters_survives(
    session, acme_support, support_ops, operator, gw
):
    """Client text is interpolated into an HTML message, so it must be escaped.

    Without escaping, "amount < 500 & rising" either loses characters or makes
    Telegram reject the whole send - which would lose the client's message
    entirely.
    """
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="amount < 500 & rising",
        sender_name="Tom <b>Baker</b>",
        telegram_message_id=779,
    )

    topic = gw.all_text_to(OPS_CHAT)
    assert "amount &lt; 500 &amp; rising" in topic
    assert "Tom &lt;b&gt;Baker&lt;/b&gt;" in topic


def test_only_these_functions_may_write_to_a_client_chat() -> None:
    """The core safety property, enforced structurally rather than by review.

    Anything reaching a client group is visible to a customer of NexterPay's
    customer. The list below is deliberately short, and every entry composes
    its own text - none of them can carry wording a member of staff typed
    except send_client_reply, which is the one route out and has a
    confirmation step in front of it.

    If this fails because you added a function, that is the point: decide
    whether it really needs to write outward, and if it does, add it here so
    the next person can see the whole list in one place.
    """
    import pathlib
    import re

    allowed = {
        "open_request",       # the acknowledgement carrying the reference
        "post_anchor",        # a fresh message to reply to, from the list
        "send_client_reply",  # the only route for staff-written words
        "relay_client_message",  # telling someone a request is already closed
        "close",              # the closure notice
    }

    source = pathlib.Path("app/services/relay.py").read_text().splitlines()
    writers, current = set(), None
    for index, line in enumerate(source):
        named = re.match(r"(?:async )?def (\w+)", line)
        if named:
            current = named.group(1)
        window = "".join(source[index:index + 4])
        if re.search(r"gateway\.send_(message|file)\(", line) and (
            "source.telegram_chat_id" in window or "source_chat.telegram_chat_id" in window
        ):
            writers.add(current)

    assert writers == allowed, (
        f"functions writing to a client chat changed.\n"
        f"  added:   {sorted(writers - allowed)}\n"
        f"  removed: {sorted(allowed - writers)}"
    )
