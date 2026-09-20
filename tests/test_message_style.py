"""How a message reads when it arrives in a counterparty's group.

NexterPay redrew every outbound message on 19 September, as a before-and-after
screenshot rather than a description, and the redraw is better than what it
replaced. The old shape ran routing and content together on one line —
`ACME-1098 — from Sarah Hill — This transaction was successfully processed` —
so the answer, which is the only part anybody wants, started two thirds of the
way through. The new shape is a header, then the message, then the invitation.

These tests exist because wording is the part of this platform with no natural
guard. A leak breaks a test; a message that reads badly breaks nothing and is
simply what every client sees until somebody complains. The screenshot is the
specification, so it is worth pinning.

One deliberate loss is recorded here rather than left to be discovered:
`staff_reply_text` no longer carries the sender's name. See the test at the
bottom.
"""

from __future__ import annotations

import pytest

from app.domain.enums import Department
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _open(session, gw, chat, subject="Settlement missing"):
    return await relay.open_request(
        session, gw,
        source_chat=chat,
        subject=subject,
        body="We have not received settlement for 3 March.",
        raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )


# --------------------------------------------------------------------------
# The markers
# --------------------------------------------------------------------------

async def test_every_outbound_message_opens_with_its_marker(
    session, acme_support, support_ops, operator, gw
):
    """A counterparty group carries ordinary conversation as well as ours.
    The marker is what makes our messages findable by scrolling."""
    item = await _open(session, gw, acme_support)
    assert gw.messages_to(CLIENT_CHAT)[0].startswith(relay.MARK_RECEIVED)

    await relay.claim(session, gw, item, Actor.of(operator))
    assert gw.messages_to(CLIENT_CHAT)[-1].startswith(relay.MARK_OWNER)

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator),
        "Settled at 14:02, reference attached.",
    )
    assert gw.messages_to(CLIENT_CHAT)[-1].startswith(relay.MARK_RESPONSE)

    await relay.close(session, gw, item, Actor.of(operator))
    assert gw.messages_to(CLIENT_CHAT)[-1].startswith(relay.MARK_RESOLVED)


def test_the_markers_are_all_different() -> None:
    """Two states sharing a symbol is worse than no symbol at all."""
    marks = [
        relay.MARK_RECEIVED, relay.MARK_RESPONSE,
        relay.MARK_RESOLVED, relay.MARK_OWNER,
    ]
    assert len(set(marks)) == len(marks)


# --------------------------------------------------------------------------
# The invitation
# --------------------------------------------------------------------------

async def test_the_reply_hint_stands_on_its_own_line(
    session, acme_support, support_ops, operator, gw
):
    """It was run on from the sentence before it, which read as part of the
    answer rather than as an instruction about how this group works."""
    await _open(session, gw, acme_support)
    ack = gw.messages_to(CLIENT_CHAT)[0]

    assert relay.REPLY_HINT in ack
    assert ack.endswith(relay.REPLY_HINT)
    assert f"\n\n{relay.REPLY_HINT}" in ack


def test_the_invitation_is_bracketed() -> None:
    """NexterPay's mockup sets it apart from the message. It is the only line
    in any of these that tells somebody what to do."""
    assert relay.REPLY_HINT.startswith("(") and relay.REPLY_HINT.endswith(")")
    assert relay.OUTSTANDING_HINT.startswith("(")
    assert relay.OUTSTANDING_HINT.endswith(")")


async def test_a_reply_invites_a_reply(
    session, acme_support, support_ops, operator, gw
):
    """The point of the redraw. Every message we send is a place the client can
    answer from, so they never have to hunt for the right one to reply to."""
    item = await _open(session, gw, acme_support)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Checking with the bank now.",
    )
    assert relay.REPLY_HINT in gw.messages_to(CLIENT_CHAT)[-1]


# --------------------------------------------------------------------------
# The header
# --------------------------------------------------------------------------

def test_a_reply_is_headed_with_the_reference() -> None:
    text = relay.staff_reply_text("ACME-1098", "This was processed. Thanks")
    first, blank, body = text.split("\n")[:3]

    assert first == f"{relay.MARK_RESPONSE} Response to ACME-1098"
    assert blank == ""
    assert body == "This was processed. Thanks"


def test_the_answer_starts_on_its_own_line() -> None:
    """The whole complaint about the old format. `ACME-1098 — from Sarah —
    <answer>` buries the answer behind two pieces of routing."""
    text = relay.staff_reply_text("ACME-1098", "Yes, that is correct.")
    assert "\n\nYes, that is correct.\n\n" in text


def test_a_tagged_contact_still_escapes_what_staff_typed() -> None:
    """The mention is markup and must not be escaped. Everything around it is
    whatever somebody typed, and a stray "<" would be swallowed as markup or
    rejected outright by Telegram."""
    text = relay.staff_reply_text(
        "ACME-1098",
        "check the <urgent> flag",
        mention='<a href="tg://user?id=7">Ann</a>',
        escape=True,
    )
    assert '<a href="tg://user?id=7">Ann</a>' in text
    assert "&lt;urgent&gt;" in text
    assert "<urgent>" not in text


# --------------------------------------------------------------------------
# Business, which reads differently on purpose
# --------------------------------------------------------------------------

async def test_business_calls_it_an_enquiry_throughout(
    session, acme_business, gw
):
    """One thing should not have two names between one message and the next.
    The Business front door says Commercial Enquiry, so everything after it
    does too."""
    item = await _open(session, gw, acme_business, subject="New corridors")

    ack = relay.acknowledgement_text(item)
    assert "Enquiry" in ack
    assert "Request" not in ack
    assert "get back to you" in ack

    notice = relay.claim_notice_text(item, "Sarah Hill")
    assert notice is not None
    assert "Enquiry" in notice
    assert item.department is Department.BUSINESS


async def test_business_names_the_team_not_the_person(
    session, acme_business, gw
):
    """A commercial conversation should not read as a queue with a named
    handler."""
    item = await _open(session, gw, acme_business, subject="New corridors")
    notice = relay.claim_notice_text(item, "Sarah Hill")
    assert "Sarah Hill" not in notice


# --------------------------------------------------------------------------
# What the redraw cost
# --------------------------------------------------------------------------

def test_a_reply_no_longer_carries_the_senders_name() -> None:
    """Recorded deliberately, because it reverses an earlier decision.

    On 5 September NexterPay asked for replies to be signed — "the client
    should know who they are speaking with, more personal". The 19 September
    mockup announces the person once, when they claim the request, and not on
    every message after it.

    That is the better trade on a long thread: a name on all twelve replies is
    noise. It does mean a second person stepping in mid-thread is not
    announced. This test fails the day somebody puts the signature back, which
    is the point — it should be a decision, not a drift.
    """
    text = relay.staff_reply_text("ACME-1098", "Done.")
    assert "from" not in text.lower().split("\n")[0]


async def test_the_person_is_still_announced_once(
    session, acme_support, support_ops, operator, gw
):
    """Which is what makes the line above acceptable rather than a loss."""
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    notice = gw.messages_to(CLIENT_CHAT)[-1]
    assert "Sarah Hill" in notice
    assert item.client_reference in notice
