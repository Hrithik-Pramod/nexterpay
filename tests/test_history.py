"""The audit trail.

The acceptance condition for day one: a work item's full history can be
reconstructed from the event log alone, with no other source consulted.

These tests also guard the decision that every state change is rendered as a
visible line - because NexterPay's audit method is a person reading the topic,
an unrendered event is an invisible one.
"""

from __future__ import annotations

from app.db.models import Event
from app.domain import work_items as wi
from app.domain.enums import EventType, Priority, WorkItemStatus
from app.domain.history import load_events, render_event, render_history
from app.domain.work_items import Actor


async def _full_lifecycle(session, chat, operator, senior, manager):
    item = await wi.create_work_item(
        session,
        source_chat=chat,
        subject="Settlement missing",
        original_message="We are missing settlement for 3 March.",
        raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )
    await wi.attach_topic(session, item, topic_id=771)
    await wi.claim(session, item, Actor.of(operator))
    await wi.change_status(session, item, WorkItemStatus.WAITING_CLIENT, Actor.of(operator))
    await wi.assign(session, item, senior, Actor.of(senior))
    await wi.change_priority(session, item, Priority.HIGH, Actor.of(senior))
    await wi.close(session, item, Actor.of(senior))
    await wi.reopen(session, item, Actor.of(manager))
    return item


async def test_history_reconstructs_from_events_alone(
    session, acme_support, support_ops, operator, senior, manager
):
    item = await _full_lifecycle(session, acme_support, operator, senior, manager)

    lines = render_history(await load_events(session, item))

    assert any("Work Item created by Tom Baker" in line for line in lines)
    assert any("Claimed by Sarah Hill" in line for line in lines)
    assert any("Waiting for Client" in line for line in lines)
    assert any("Assigned to James Okoro" in line for line in lines)
    assert any("Priority: Medium → High" in line for line in lines)
    assert any("Closed by James Okoro" in line for line in lines)
    assert any("Reopened by Priya Nair" in line for line in lines)


async def test_status_change_renders_both_sides(
    session, acme_support, support_ops, operator
):
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="Login issue",
        original_message="Cannot log in.",
        raised_by_name="Tom Baker",
    )
    await wi.change_status(session, item, WorkItemStatus.IN_PROGRESS, Actor.of(operator))

    events = await load_events(session, item)
    line = render_event(events[-1])
    assert line == "Status: Open → In Progress (Sarah Hill)"


async def test_events_are_ordered_and_never_mutated(
    session, acme_support, support_ops, operator, senior, manager
):
    item = await _full_lifecycle(session, acme_support, operator, senior, manager)

    ids = [e.id for e in await load_events(session, item)]
    assert ids == sorted(ids), "events must read back in the order they occurred"

    # An event carries no update path - confirm the model exposes none.
    assert not hasattr(Event, "updated_at")


async def test_every_event_type_has_a_renderer():
    """Guards the contract in history.py: a new EventType without a renderer
    would silently produce an invisible entry in NexterPay's audit trail."""
    from datetime import datetime, timezone

    for event_type in EventType:
        event = Event(
            work_item_id=1,
            event_type=event_type,
            actor_name="Test User",
            payload={
                "assignee": "Someone",
                "from_label": "Open",
                "to_label": "Closed",
                "client": "Acme",
                "kind": "document",
            },
            created_at=datetime.now(timezone.utc),  # noqa: UP017
        )
        rendered = render_event(event)
        assert rendered and isinstance(rendered, str)


async def test_history_quotes_what_was_actually_said(
    session, acme_support, support_ops, operator
):
    """The history has to contain the words, not just who said something.

    Reported in UAT: an internal note was added and could not be found in the
    history. It was recorded correctly - the renderer simply never printed the
    text, so every note looked identical. NexterPay have no other interface
    onto this data, so a trail of "Internal note by peter" is worthless.
    """
    from app.services import relay
    from app.services.gateway import FakeGateway

    gw = FakeGateway()
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing settlement.", raised_by_name="Tom Baker",
    )
    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.add_internal_note(
        session, gw, item, Actor.of(operator), "chased the acquirer, waiting on them"
    )
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "we are on it, update tomorrow."
    )
    await relay.relay_client_message(
        session, gw, item, text="any news?", sender_name="Tom Baker",
        telegram_message_id=99,
    )

    history = "\n".join(render_history(await load_events(session, item)))

    assert "chased the acquirer, waiting on them" in history
    assert "we are on it, update tomorrow." in history
    assert "any news?" in history


async def test_topic_announcements_stay_terse(
    session, acme_support, support_ops, operator
):
    """The topic already contains the message, so the marker must not repeat it."""
    from app.services import relay
    from app.services.gateway import FakeGateway

    gw = FakeGateway()
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing settlement.", raised_by_name="Tom Baker",
    )
    await relay.add_internal_note(
        session, gw, item, Actor.of(operator), "chased the acquirer"
    )

    announcements = [
        c.payload["text"] for c in gw.calls
        if c.method == "send_message" and c.payload["text"].startswith("•")
    ]
    assert any("Internal note by" in a for a in announcements)
    assert not any("chased the acquirer" in a for a in announcements)


async def test_long_notes_are_shortened_not_dropped(
    session, acme_support, support_ops, operator
):
    from app.services import relay
    from app.services.gateway import FakeGateway

    gw = FakeGateway()
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing settlement.", raised_by_name="Tom Baker",
    )
    await relay.add_internal_note(
        session, gw, item, Actor.of(operator), "detail " * 200
    )

    line = [
        line for line in render_history(await load_events(session, item))
        if "Internal note" in line
    ][0]
    assert "detail" in line
    assert "…" in line
    assert len(line) < 300
