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

The redraw changed the shape and nothing else. The first attempt also dropped
the sender's name, on the strength of the mockup showing none — two existing
tests failed and were right to, because NexterPay had asked for signed replies
twice. The name moved into the header instead. See the test at the bottom.
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


def test_the_invitation_is_bracketed_and_set_apart() -> None:
    """NexterPay's mockup sets it apart from the message, and on 20 September
    they asked for the nesting: italic for the aside, bold for the instruction
    inside it. The brackets say "this is not the answer"; the bold says "this
    is the part that matters"."""
    for hint in (relay.REPLY_HINT, relay.OUTSTANDING_HINT):
        assert hint.startswith("<i>(")
        assert hint.endswith(")</i>")
        assert "<b>reply to this message</b>" in hint


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
    text = relay.staff_reply_text(
        "ACME-1098", "This was processed. Thanks", sender="Sarah Hill",
    )
    first, blank, body = text.split("\n")[:3]

    assert first == (
        f"{relay.MARK_RESPONSE} <b>Response to ACME-1098 — from Sarah Hill</b>"
    )
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
# What the redraw did not change
# --------------------------------------------------------------------------

def test_a_reply_still_carries_the_senders_name() -> None:
    """The redraw changed the shape, not who the message is from.

    The first version of this dropped the name, because the mockup shows
    `Response to ACME-1098` with nothing after it. Two existing tests failed
    and they were right to. NexterPay asked for signed replies on 5 September —
    "the client should know who they are speaking with, more personal" — and
    again for Business, where a negotiation is the most personal conversation
    on the platform.

    A drawn example of a single message is not where a reversal of that should
    be read in. The name moved into the header instead.
    """
    text = relay.staff_reply_text("ACME-1098", "Done.", sender="Sarah Hill")
    header = text.split("\n")[0]

    assert "from Sarah Hill" in header
    assert header.startswith(relay.MARK_RESPONSE)
    # Still the header, not the answer: the message begins on its own line.
    assert text.split("\n")[2] == "Done."


async def test_the_person_is_still_announced_once(
    session, acme_support, support_ops, operator, gw
):
    """Which is what makes the line above acceptable rather than a loss."""
    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    notice = gw.messages_to(CLIENT_CHAT)[-1]
    assert "Sarah Hill" in notice
    assert item.client_reference in notice


# --------------------------------------------------------------------------
# Formatting, and the price of it
#
# NexterPay, 20 September: titles bold, the invitation italic with "reply to
# this message" bold inside it, and the client's quoted words in italics.
#
# Turning HTML on changes the rules for everything, which is why the tests
# below are mostly about escaping rather than about tags. A message with an
# unescaped "<" in it is not a message that looks wrong - Telegram rejects it
# outright, so it is a message the client never receives, and nothing on our
# side reports a failure the client can see.
# --------------------------------------------------------------------------

BRUTAL = 'amount < 500 & rising > "urgent" <b>not bold</b>'


def test_the_titles_are_bold(session) -> None:
    text = relay.staff_reply_text("ACME-1098", "ok", sender="Sarah Hill")
    assert text.split("\n")[0].count("<b>") == 1
    assert text.split("\n")[0].endswith("</b>")


async def test_a_clients_angle_brackets_survive_their_own_closure(
    session, acme_support, support_ops, operator, gw
):
    """The most likely place on the platform for this to bite.

    A closure repeats what the client originally wrote, back to them. So the
    one string guaranteed to contain whatever a client felt like typing is
    also the one we hand to Telegram as markup.
    """
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Amounts",
        body=BRUTAL, raised_by_name="Tom Baker",
    )
    await relay.close(session, gw, item, Actor.of(operator))

    closure = gw.messages_to(CLIENT_CHAT)[-1]
    assert "&lt;" in closure and "&amp;" in closure
    assert "<b>not bold</b>" not in closure, (
        "a client typed <b> and it was passed through as markup"
    )


async def test_staff_typing_angle_brackets_is_escaped_too(
    session, acme_support, support_ops, operator, gw
):
    """Every reply is HTML now, not only the ones that tag a contact. That was
    the change most likely to go unnoticed: the tagged path had always escaped
    and the ordinary path had never needed to."""
    item = await _open(session, gw, acme_support)
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), BRUTAL,
    )
    sent = gw.messages_to(CLIENT_CHAT)[-1]
    assert "&lt;" in sent
    assert "<b>not bold</b>" not in sent


def test_a_name_with_an_ampersand_does_not_break_the_header() -> None:
    """Staff display names are set by NexterPay and "Smith & Co" is an
    ordinary thing to call someone."""
    text = relay.staff_reply_text("ACME-1098", "ok", sender="Smith & Co")
    assert "Smith &amp; Co" in text
    assert "Smith & Co" not in text


async def test_a_resolution_note_is_escaped(
    session, acme_support, support_ops, operator, gw
):
    """Typed by staff at the moment of closing, and it goes straight out."""
    item = await _open(session, gw, acme_support)
    await relay.close(
        session, gw, item, Actor.of(operator), resolution="refunded < 24h & confirmed",
    )
    closure = gw.messages_to(CLIENT_CHAT)[-1]
    assert "&lt; 24h &amp; confirmed" in closure


def test_every_tag_we_open_is_closed() -> None:
    """Telegram rejects unbalanced markup, so an unclosed tag is a message
    that never arrives rather than one that looks odd."""

    samples = [
        relay.REPLY_HINT,
        relay.OUTSTANDING_HINT,
        relay.staff_reply_text("ACME-1", "body", sender="Sarah"),
    ]
    for sample in samples:
        for tag in ("b", "i"):
            assert sample.count(f"<{tag}>") == sample.count(f"</{tag}>"), sample


# --------------------------------------------------------------------------
# Markup and parse mode must agree, on every path
#
# Found on 20 September, in production, by reading the code. `open_outbound`
# set parse_mode only inside its tag-a-contact branch. That was right until
# the opening message gained a bold title that morning, and wrong from the
# moment it did: an untagged opening - which is every rate check and every
# request NexterPay raise - would have arrived at the counterparty with the
# tags showing as text.
#
# There was already a check meant to catch exactly this. It scanned the source
# for parse_mode="HTML" within a few lines of each send, found the one in the
# branch below the default, and reported the function as fine. A test that
# looks near the right place is not the same as one that looks at it.
#
# So this is the invariant instead, asserted on what the gateway actually
# received: if a message carries tags, it was sent as HTML. It does not care
# how the code is arranged, which is the point.
# --------------------------------------------------------------------------

def _markup_matches_parse_mode(gw) -> list[str]:
    """Every send where tags and parse mode disagree, in either direction."""
    wrong = []
    for call in gw.calls:
        if call.method not in ("send_message", "edit_message_text"):
            continue
        text = call.payload.get("text") or ""
        tagged = "<b>" in text or "<i>" in text or "<a href=" in text
        as_html = call.payload.get("parse_mode") == "HTML"
        if tagged and not as_html:
            wrong.append(f"tags sent as plain text: {text[:90]!r}")
    return wrong


async def test_nothing_sends_tags_as_plain_text(
    session, acme_support, support_ops, operator, pexi_supplier, gw
):
    """Drives the paths that compose HTML and checks every send the gateway
    saw. A message that fails this does not look wrong to a client — it shows
    them `<b>` and `</i>`, which looks broken."""
    from app.services.relay import open_outbound, post_anchor

    item = await _open(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Looking at it now.",
    )
    await post_anchor(session, gw, item)
    await relay.close(session, gw, item, Actor.of(operator))

    # The one that was broken: NexterPay raising something themselves.
    await open_outbound(
        session, gw,
        counterparty_chat=pexi_supplier,
        subject="Rate check",
        body="Could you send your current rate?",
        actor=Actor.of(operator),
    )

    assert _markup_matches_parse_mode(gw) == []


async def test_an_outbound_opening_is_sent_as_html(
    session, support_ops, operator, pexi_supplier, gw
):
    """Named separately from the sweep above, because this is the one that
    shipped broken and a general test passing tells you less than a specific
    one that is about the actual fault."""
    from app.services.relay import open_outbound

    await open_outbound(
        session, gw,
        counterparty_chat=pexi_supplier,
        subject="Rate check",
        body="Could you send your current rate?",
        actor=Actor.of(operator),
    )

    sends = [
        call for call in gw.calls
        if call.method == "send_message"
        and call.chat_id == pexi_supplier.telegram_chat_id
    ]
    assert sends, "nothing reached the supplier"
    assert sends[0].payload["parse_mode"] == "HTML"
    assert "<b>" in sends[0].payload["text"]
