"""Asking another department, rather than moving a request to it.

NexterPay's answer to "transfer a request between desks", and a better one
than the question. Dragging a live request across means carrying its topic,
its history and the client's view of it into another Operations Group and
hoping all three arrive intact. Opening a fresh request instead reuses
everything that already works, and linking the two means the client still sees
one thread while two desks work on it.

The property this file guards: nothing about it reaches the counterparty. The
client raised one thing. That NexterPay asked Finance about it is internal.
"""

from __future__ import annotations

import pytest

from app.bot.registry import register_operations_chat
from app.domain.enums import Department
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPORT_OPS = -1001000000001
FINANCE_OPS = -1001000000008


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
async def finance_ops(session):
    return await register_operations_chat(
        session, telegram_chat_id=FINANCE_OPS,
        department=Department.FINANCE, title="Finance Operations",
    )


async def _origin(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement not received",
        body="The 3 March settlement has not arrived.", raised_by_name="Tom Baker",
    )


async def test_it_opens_on_the_other_desk_not_this_one(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    origin = await _origin(session, gw, acme_support)
    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
    )

    assert asked.department is Department.FINANCE
    assert asked.operations_chat_id == finance_ops.id
    assert asked.topic_id in gw.topics[FINANCE_OPS]
    assert "Can you confirm the 3 March rate?" in gw.all_text_to(FINANCE_OPS)


async def test_the_client_is_told_nothing(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """The whole point of asking rather than transferring is that the client's
    view does not move. It should not even flicker."""
    origin = await _origin(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before, "the client was written to"
    assert "Finance" not in gw.all_text_to(CLIENT_CHAT)
    assert "confirm the 3 March rate" not in gw.all_text_to(CLIENT_CHAT)


async def test_the_two_are_linked_without_anyone_remembering_to(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """A link nobody makes is not a link, and both halves being visible is
    the entire reason for raising rather than transferring."""
    from app.domain import work_items as wi

    origin = await _origin(session, gw, acme_support)
    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert [i.id for i in await wi.linked_to(session, origin)] == [asked.id]
    assert [i.id for i in await wi.linked_to(session, asked)] == [origin.id]


async def test_it_keeps_the_client_and_the_supplier_filing(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Same client, so the code and the filing structure still hold - which is
    what makes "everything for Acme" answerable across desks."""
    from app.db.models import Client

    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    origin = await _origin(session, gw, acme_support)
    supplier = Client(name="Supplier Pexi Ltd", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, origin, supplier, Actor.of(operator))

    asked = await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert asked.client_id == origin.client_id
    assert asked.client_code == "ACME"
    assert asked.supplier_code == "SPEX", "the supplier context did not follow"
    # And still nothing about SPEX reached Acme.
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)


async def test_the_original_is_untouched(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Asking is not transferring. The request stays where it was, with the
    same owner and the same status."""
    origin = await _origin(session, gw, acme_support)
    await relay.claim(session, gw, origin, Actor.of(operator))
    was_status, was_owner, was_ops = origin.status, origin.owner_staff_id, origin.operations_chat_id

    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Rate please.", actor=Actor.of(operator),
    )

    assert origin.status is was_status
    assert origin.owner_staff_id == was_owner
    assert origin.operations_chat_id == was_ops


async def test_someone_who_is_not_staff_cannot_ask(
    session, acme_support, support_ops, finance_ops, gw
):
    origin = await _origin(session, gw, acme_support)
    with pytest.raises(NotAuthorised):
        await relay.open_internal(
            session, gw, origin=origin, department=Department.FINANCE,
            subject="x", body="x", actor=Actor(name="Someone"),
        )


async def test_the_department_it_is_already_on_is_not_offered() -> None:
    """Asking your own desk is not a thing, and offering it invites a request
    that lands back where it started."""
    from app.bot import keyboards as kb

    others = [d for d in Department if d is not Department.SUPPORT]
    labels = [
        b.text for row in kb.department_choices(1, others).inline_keyboard for b in row
    ]
    assert "Support" not in labels
    assert "Finance" in labels


# --------------------------------------------------------------------------
# Answering back
#
# The half that was missing, found in live testing on 5 September. Asking
# another department opened a linked request on their desk and stopped there:
# the answer sat in their topic, and whoever asked had to know to go and read
# it. NexterPay chose asking over transferring precisely because the answer
# comes back, so a version that does not is not the feature they agreed to.
#
# It had been documented as working - in a bullet list and in a test script -
# before it existed, which is worse than the gap itself.
# --------------------------------------------------------------------------


async def _asked(session, gw, chat, operator, department=Department.FINANCE):
    """Exactly as the handler calls it, keyboard and all.

    The keyboard matters. It is built from the new request's id, and passing
    a ready-made one is how every button on every asked request came to point
    at work item zero. A helper that skipped it would leave that untested.
    """
    from app.bot import keyboards as kb

    origin = await _origin(session, gw, chat)
    asked = await relay.open_internal(
        session, gw, origin=origin, department=department,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
        keyboard_for=lambda new_id: kb.work_item_actions(
            new_id, claimed=False, asked_from=origin.display_reference
        ),
    )
    return origin, asked


async def test_it_remembers_which_request_it_was_asked_from(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Recorded outright rather than inferred from the link.

    Links are unordered and a request may hold several, so "which one do I
    answer?" has no answer from the link table - it would have to be guessed
    from the ids, which is right until somebody links a third ticket.
    """
    origin, asked = await _asked(session, gw, acme_support, operator)

    assert asked.asked_from_id == origin.id
    assert origin.asked_from_id is None, "the origin was not asked from anything"


async def test_the_answer_lands_in_the_topic_that_asked(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    origin, asked = await _asked(session, gw, acme_support, operator)

    returned = await relay.answer_internal(
        session, gw, asked, Actor.of(operator), "The rate was 1.1642, confirmed."
    )

    assert returned is not None and returned.id == origin.id
    support = gw.all_text_to(SUPPORT_OPS)
    assert "The rate was 1.1642, confirmed." in support
    assert "Finance answered" in support


async def test_the_answer_never_reaches_the_client(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """The whole point of asking rather than transferring. The client asked
    Support one question; that Finance was consulted, and what Finance said,
    are internal facts."""
    _, asked = await _asked(session, gw, acme_support, operator)

    await relay.answer_internal(
        session, gw, asked, Actor.of(operator),
        "Our cost was 1.1601 - do not quote that.",
    )

    client = gw.all_text_to(CLIENT_CHAT)
    assert "1.1601" not in client
    assert "answered" not in client.lower()


async def test_an_answer_cannot_break_the_markup(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """It is wrapped in a blockquote, so it is escaped first."""
    _, asked = await _asked(session, gw, acme_support, operator)

    await relay.answer_internal(
        session, gw, asked, Actor.of(operator), "rate is <b>1.16</b> </blockquote>",
    )
    support = gw.all_text_to(SUPPORT_OPS)

    assert "&lt;b&gt;1.16&lt;/b&gt;" in support
    assert "&lt;/blockquote&gt;" in support


async def test_answering_something_nobody_asked_is_refused_quietly(
    session, acme_support, support_ops, operator, gw
):
    """A request raised directly has nothing to answer back to. Returns None
    rather than raising - tapping Answer on an ordinary request is a
    reasonable mistake, not an error."""
    origin = await _origin(session, gw, acme_support)

    assert await relay.answer_internal(
        session, gw, origin, Actor.of(operator), "hello?"
    ) is None


async def test_both_sides_record_the_answer(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """On the answering request because that is what it was for, and on the
    origin because somebody reading its history a month later should not have
    to open another ticket to find the answer."""
    from app.domain.history import load_events, render_history

    origin, asked = await _asked(session, gw, acme_support, operator)
    await relay.answer_internal(
        session, gw, asked, Actor.of(operator), "Confirmed at 1.1642."
    )

    origin_history = "\n".join(render_history(await load_events(session, origin)))
    asked_history = "\n".join(render_history(await load_events(session, asked)))

    assert f"Answer received from {asked.display_reference}" in origin_history
    assert f"Answered {origin.display_reference}" in asked_history


async def test_the_owner_of_the_asking_request_is_told(
    session, acme_support, support_ops, finance_ops, operator, senior, gw
):
    """An answer nobody is told about is the same problem one step along - it
    moves from the wrong topic to the right topic and is still not read."""
    origin, asked = await _asked(session, gw, acme_support, operator)
    await relay.claim(session, gw, origin, Actor.of(senior))

    await relay.answer_internal(session, gw, asked, Actor.of(operator), "Confirmed.")
    assert f'tg://user?id={senior.telegram_user_id}' in gw.all_text_to(SUPPORT_OPS)


async def test_the_asked_request_offers_answer_and_not_reply_to_client(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """Finance writing straight to Acme about ACME-1038 would quote a
    reference Acme has never seen, about a question Acme never asked, from a
    desk they never contacted.

    Asserted on the keyboard rather than the flow, because the button existing
    at all is the problem.
    """
    from app.bot import keyboards as kb

    origin, asked = await _asked(session, gw, acme_support, operator)

    labels = [
        b.text
        for row in kb.work_item_actions(
            asked.id, claimed=False, asked_from=origin.display_reference
        ).inline_keyboard
        for b in row
    ]
    assert any("Answer" in t for t in labels)
    assert not any("Reply to client" in t for t in labels)

    ordinary = [
        b.text
        for row in kb.work_item_actions(origin.id, claimed=False).inline_keyboard
        for b in row
    ]
    assert any("Reply to client" in t for t in ordinary)
    assert not any("Answer" in t for t in ordinary)


async def test_the_buttons_point_at_the_request_they_are_on(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """They pointed at work item zero.

    open_internal was handed a keyboard built before the row existed, so every
    button on every request opened this way encoded id 0 and did nothing.
    Silent, like all the worst ones.
    """
    origin, asked = await _asked(session, gw, acme_support, operator)

    # Asked of the live state, not of the call log. A test that searched the
    # log for the moment a keyboard was attached would find it and pass -
    # which is exactly what happened, while the buttons were being stripped
    # again a few lines later.
    buttons = gw.live_buttons_to(FINANCE_OPS)
    assert buttons, "the asked request has no buttons on it"

    for label, data in buttons:
        assert data is not None, f"{label!r} has no callback data"
        assert data.endswith(f":{asked.id}") or f":{asked.id}:" in data, (
            f"{label!r} points at {data!r}, not at {asked.display_reference}"
        )
    assert not any(d.endswith(":0") for _, d in buttons), (
        "a button still points at work item zero"
    )


async def test_the_buttons_survive_the_header_being_refreshed(
    session, acme_support, support_ops, finance_ops, operator, senior, gw
):
    """The bug NexterPay found, as a test.

    The buttons were put on the header. `link` runs immediately afterwards and
    calls `refresh_header`, which calls `edit_message_text` with no
    reply_markup - and Telegram reads that as "this message has no keyboard
    now". Attached and removed inside one function.

    Refreshing happens constantly: every claim, status change, priority
    change and link rewrites the header. So this claims the request first,
    which is the commonest of them, and then looks again.
    """
    origin, asked = await _asked(session, gw, acme_support, operator)
    assert gw.live_buttons_to(FINANCE_OPS), "gone before anything even happened"

    await relay.claim(session, gw, asked, Actor.of(senior))

    labels = [label for label, _ in gw.live_buttons_to(FINANCE_OPS)]
    assert labels, "the header refresh took the buttons off"
    assert any("Answer" in t for t in labels)


async def test_the_client_s_original_request_travels_with_the_question(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """NexterPay, 5 September: "only shows my comments not original from
    clients request".

    Finance were being asked to confirm a rate with no sight of why anyone
    wanted it, so the first thing they did was go and open the other ticket -
    which is precisely the work this feature exists to save.
    """
    origin = await relay.open_request(
        session, gw, source_chat=acme_support,
        subject="Settlement not received",
        body="The 3 March settlement has not arrived and our client is chasing.",
        raised_by_name="Tom Baker",
    )
    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Rate check", body="Can you confirm the 3 March rate?",
        actor=Actor.of(operator),
    )

    finance = gw.all_text_to(FINANCE_OPS)
    assert "Can you confirm the 3 March rate?" in finance, "the question is missing"
    assert "The 3 March settlement has not arrived" in finance, (
        "the client's own request did not travel with it"
    )
    assert "Tom Baker" in finance, "whoever raised it is not named"
    assert origin.display_reference in finance


async def test_a_very_long_original_is_trimmed(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """A client who writes four paragraphs must not push the question off the
    screen. The full text is one tap away in the linked request."""
    origin = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Long one",
        body="word " * 400, raised_by_name="Tom Baker",
    )
    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Check", body="Please look at this.", actor=Actor.of(operator),
    )

    context = [
        t for t in gw.messages_to(FINANCE_OPS) if "asked Finance about" in t
    ][-1]
    assert len(context) < 1200, f"{len(context)} characters of context"
    assert "…" in context, "it was cut without saying so"
    assert "Please look at this." in context, "the question was crowded out"


async def test_an_outbound_origin_says_we_raised_it(
    session, acme_support, support_ops, finance_ops, operator, gw
):
    """"Tom Baker raised it" would be wrong on a request NexterPay opened
    themselves, and the header already makes this distinction."""
    origin = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Chase",
        body="Chasing the March file.", raised_by_name="peter",
    )
    origin.raised_by_us = True
    await session.flush()

    await relay.open_internal(
        session, gw, origin=origin, department=Department.FINANCE,
        subject="Check", body="Anything on this?", actor=Actor.of(operator),
    )

    context = [
        t for t in gw.messages_to(FINANCE_OPS) if "asked Finance about" in t
    ][-1]
    assert "we raised it" in context
