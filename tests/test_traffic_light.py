"""The colour on the front of every topic title.

NexterPay asked for red / amber / green in the topic list so a desk can be
read at a glance. Telegram fixes a topic's colour at creation and will not let
a bot change it afterwards, so the light lives in the name instead - which is
also where it is never truncated, because the list cuts from the right.
"""

from __future__ import annotations

import pytest

from app.domain.enums import WorkItemStatus
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


def _title(gw: FakeGateway, item) -> str:
    return gw.topic_names[(OPS_CHAT, item.topic_id)]


async def _raised(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="No settlement for 3 March.", raised_by_name="Tom Baker",
    )


async def test_a_new_request_is_red(session, acme_support, support_ops, gw):
    item = await _raised(session, gw, acme_support)
    assert relay.traffic_light(item) == relay.LIGHT_UNCLAIMED
    assert _title(gw, item).startswith(relay.LIGHT_UNCLAIMED)


async def test_claiming_turns_it_amber(session, acme_support, support_ops, operator, gw):
    item = await _raised(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    assert relay.traffic_light(item) == relay.LIGHT_WORKING
    assert _title(gw, item).startswith(relay.LIGHT_WORKING)


async def test_completed_stays_amber(session, acme_support, support_ops, operator, gw):
    """Asked directly and answered directly: green means closed, nothing else.

    Completed is work finished but not archived. It is tempting to call that
    green, and NexterPay said no.
    """
    item = await _raised(session, gw, acme_support)
    await relay.change_status(
        session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator)
    )

    assert item.status is WorkItemStatus.COMPLETED
    assert relay.traffic_light(item) == relay.LIGHT_WORKING


async def test_closing_turns_it_green_before_the_topic_is_archived(
    session, acme_support, support_ops, manager, gw
):
    """The ordering trap.

    A closed topic is archived, and renaming one afterwards is at best
    unreliable. If the retitle happened after the archive, every finished
    request would sit in the list showing amber forever - the one colour that
    would be permanently wrong.
    """
    item = await _raised(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(manager))

    assert _title(gw, item).startswith(relay.LIGHT_DONE)

    renames = [i for i, c in enumerate(gw.calls) if c.method == "rename_topic"]
    closes = [i for i, c in enumerate(gw.calls) if c.method == "close_topic"]
    assert renames and closes
    assert max(renames) < min(closes), "the topic was archived before it went green"


async def test_reopening_turns_it_back(
    session, acme_support, support_ops, manager, gw
):
    item = await _raised(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(manager))
    await relay.reopen(session, gw, item, Actor.of(manager))

    assert relay.traffic_light(item) != relay.LIGHT_DONE
    assert _title(gw, item).startswith(relay.LIGHT_WORKING)


async def test_the_reference_survives_the_light(
    session, acme_support, support_ops, gw
):
    """Whatever else the title carries, it still has to be findable by
    reference - searching the topic list is how anyone finds anything."""
    item = await _raised(session, gw, acme_support)
    title = _title(gw, item)

    assert item.display_reference in title
    assert "Acme Payments" in title
    assert len(title) <= 128, "Telegram caps a topic name at 128 characters"


async def test_topics_are_created_with_one_neutral_colour() -> None:
    """Otherwise Telegram picks at random and the dots mean nothing, which
    leaves two colour schemes in the same list competing for attention."""
    import inspect

    from app.services import gateway as gw_module

    source = inspect.getsource(gw_module.AiogramGateway.create_topic)
    assert "icon_color=NEUTRAL_TOPIC_COLOUR" in source
    # Telegram accepts six values and rejects anything else.
    assert gw_module.NEUTRAL_TOPIC_COLOUR in (
        0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F
    )


# --------------------------------------------------------------------------
# Priority, marked rather than coloured
# --------------------------------------------------------------------------

def test_telegram_has_no_font_colour_so_urgency_is_a_mark() -> None:
    """NexterPay asked for High priority in red font.

    Telegram's whole set of text styles is bold, italic, underline,
    strikethrough, spoiler, code, pre, blockquote and links. There is no
    colour entity, so red text is not available at any price - the emphasis
    has to be a character instead.
    """
    from app.domain.enums import Priority

    assert relay.PRIORITY_MARKS[Priority.CRITICAL]
    assert relay.PRIORITY_MARKS[Priority.HIGH]
    assert Priority.MEDIUM not in relay.PRIORITY_MARKS
    assert Priority.LOW not in relay.PRIORITY_MARKS


def test_the_priority_mark_never_borrows_the_traffic_light() -> None:
    """Red already means "nobody has picked this up".

    Reusing it for urgency would leave one colour meaning two things in the
    same topic, which is worse than leaving urgency unmarked.
    """
    lights = {relay.LIGHT_UNCLAIMED, relay.LIGHT_WORKING, relay.LIGHT_DONE}
    assert not (set(relay.PRIORITY_MARKS.values()) & lights)


async def test_an_urgent_request_says_so_in_the_header(
    session, acme_support, support_ops, operator, gw
):
    from app.domain.enums import Priority

    item = await _raised(session, gw, acme_support)
    header = relay.header_text(item, "Acme Payments")
    assert "Medium" in header
    assert relay.PRIORITY_MARKS[Priority.HIGH] not in header

    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(operator))
    header = relay.header_text(item, "Acme Payments")
    assert relay.PRIORITY_MARKS[Priority.CRITICAL] in header
    assert "Critical" in header


async def test_an_urgent_request_says_so_in_the_topic_list_too(
    session, acme_support, support_ops, operator, gw
):
    """The header alone is not enough, and shipping it that way was a miss.

    Triage happens in the topic list. A mark that only appears once you have
    opened a request tells you something you no longer needed to be told -
    you are already reading the thing. Found on 4 September when a High
    request sat in the list looking exactly like every Normal one.
    """
    from app.domain.enums import Priority

    item = await _raised(session, gw, acme_support)
    assert relay.PRIORITY_MARKS[Priority.HIGH] not in _title(gw, item)

    await relay.change_priority(session, gw, item, Priority.HIGH, Actor.of(operator))
    title = _title(gw, item)

    assert relay.PRIORITY_MARKS[Priority.HIGH] in title
    # Still red: raising the priority does not claim it. Red-plus-mark is the
    # combination worth spotting - urgent, and nobody has picked it up.
    assert title.startswith(relay.LIGHT_UNCLAIMED), "the light still comes first"
    assert item.display_reference in title, "and the reference survives both"


async def test_dropping_the_priority_takes_the_mark_back_off(
    session, acme_support, support_ops, operator, gw
):
    """A mark that goes on and never comes off is worse than none.

    Every topic would drift to urgent, and a list where everything is urgent
    sorts no better than a list where nothing is.
    """
    from app.domain.enums import Priority

    item = await _raised(session, gw, acme_support)
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(operator))
    assert relay.PRIORITY_MARKS[Priority.CRITICAL] in _title(gw, item)

    await relay.change_priority(session, gw, item, Priority.LOW, Actor.of(operator))
    title = _title(gw, item)
    for mark in relay.PRIORITY_MARKS.values():
        assert mark not in title, f"{mark} was left behind"


async def test_a_marked_title_still_fits_telegrams_limit(
    session, acme_support, support_ops, operator, gw
):
    """The mark is added to a string that was already being truncated at 128.

    Worth its own test because the failure is not a wrong title, it is
    Telegram rejecting the rename outright - and the rename is what carries
    the traffic light, so the light would silently stop moving on exactly the
    requests that matter most.
    """
    from app.domain.enums import Priority

    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="S" * 200,
        body="Long.", raised_by_name="Tom Baker",
    )
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(operator))

    assert len(_title(gw, item)) <= 128


async def test_a_closed_request_drops_its_priority_mark(
    session, acme_support, support_ops, manager, gw
):
    """Green and urgent are contradictory instructions.

    NexterPay saw a closed request still carrying its mark in the archived
    list after the 4 September backfill. Urgency is a claim about what to do
    next, and on a closed request there is nothing next - so a list of
    finished work full of exclamation marks only teaches people to read past
    them, including on the open ones where they mean something.
    """
    from app.domain.enums import Priority

    item = await _raised(session, gw, acme_support)
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(manager))
    assert relay.PRIORITY_MARKS[Priority.CRITICAL] in _title(gw, item)

    await relay.close(session, gw, item, Actor.of(manager))
    title = _title(gw, item)

    assert title.startswith(relay.LIGHT_DONE)
    for mark in relay.PRIORITY_MARKS.values():
        assert mark not in title, f"a closed request is still shouting: {title}"


async def test_reopening_brings_the_mark_back(
    session, acme_support, support_ops, manager, gw
):
    """Suppressing it on close must not lose it. A reopened Critical request
    is Critical again, and would otherwise sit in the list looking routine."""
    from app.domain.enums import Priority

    item = await _raised(session, gw, acme_support)
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(manager))
    await relay.close(session, gw, item, Actor.of(manager))
    await relay.reopen(session, gw, item, Actor.of(manager))

    assert relay.PRIORITY_MARKS[Priority.CRITICAL] in _title(gw, item)


async def test_the_people_in_the_header_are_tappable(
    session, acme_support, support_ops, operator, gw
):
    """NexterPay's mock showed @names rather than plain text, and they were
    right - the header is where somebody looks when they need the owner
    rather than the request, and a name you can tap is a person you can
    reach."""
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )
    await relay.claim(session, gw, item, Actor.of(operator))

    header = gw.current_text(item.header_message_id)
    assert 'tg://user?id=9001' in header, "the raiser is not tappable"
    assert f'tg://user?id={operator.telegram_user_id}' in header, "the owner is not"


def test_a_person_with_no_telegram_id_gets_a_name_not_a_dead_link() -> None:
    """Counterparties added by /npaddparty have no Telegram identity at all."""
    assert relay._mention("Someone", None) == "Someone"
    assert relay._mention("A <b>name</b>", None) == "A &lt;b&gt;name&lt;/b&gt;"
