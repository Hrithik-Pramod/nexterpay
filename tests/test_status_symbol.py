"""The stage symbol on a topic title, beside the traffic light.

NexterPay mocked up coloured bubbles with symbols inside them on 14 September —
a tick, circular arrows, a cross — and asked for those *and* the blue bubble
*and* the traffic light.

Telegram gives a topic either a coloured bubble or a custom emoji icon, never
both, and the colour is fixed when the topic is created and cannot be edited
afterwards. So the bubble could carry a symbol only by giving up the blue. Told
that, they chose to keep the blue and put the symbol on the name, where the
light already lives.

Two of the three symbols they drew do not exist: `getForumTopicIconStickers`
has ✅ but no 🔄 and no ❌. The four used here were picked from what the set
actually contains, so the vocabulary still transfers if they ever change their
mind about the bubble.

The last test in this file is the one that holds their decision: the topic is
still created blue.
"""

from __future__ import annotations

import ast
import inspect

from app.domain.enums import Priority, WorkItemStatus
from app.services import relay

SYMBOLS = {
    relay.SYMBOL_UNCLAIMED,
    relay.SYMBOL_WORKING,
    relay.SYMBOL_RESOLVED,
    relay.SYMBOL_CLOSED,
}


class _Item:
    """Enough of a work item for a title. No database needed to render text."""

    def __init__(
        self,
        status: WorkItemStatus,
        *,
        owner: int | None = None,
        priority: Priority = Priority.MEDIUM,
        reference: str = "ACME-1067",
        subject: str = "Settlement missing",
    ) -> None:
        self.status = status
        self.owner_staff_id = owner
        self.priority = priority
        self.display_reference = reference
        self.subject = subject


# --------------------------------------------------------------------------
# The four stages
# --------------------------------------------------------------------------

def test_nobody_has_picked_it_up() -> None:
    assert relay.status_symbol(_Item(WorkItemStatus.OPEN)) == relay.SYMBOL_UNCLAIMED


def test_claimed_is_being_worked_on() -> None:
    """Claimed with an owner is somebody's problem now, which is the point."""
    assert relay.status_symbol(
        _Item(WorkItemStatus.OPEN, owner=7)
    ) == relay.SYMBOL_WORKING


def test_resolved_and_closed_are_told_apart() -> None:
    """The light calls one amber and the other green, and NexterPay were firm
    that green means closed and nothing else. But "we have fixed it" and "this
    is over" are still different things to somebody scanning the list."""
    assert relay.status_symbol(
        _Item(WorkItemStatus.COMPLETED)
    ) == relay.SYMBOL_RESOLVED
    assert relay.status_symbol(_Item(WorkItemStatus.CLOSED)) == relay.SYMBOL_CLOSED


def test_everything_in_flight_reads_as_in_progress() -> None:
    """Amber covers five statuses. So does the symbol — deliberately. A
    vocabulary of four is the whole point; a symbol per status would be a
    second status field that nobody asked for."""
    for status in (
        WorkItemStatus.CLAIMED,
        WorkItemStatus.IN_PROGRESS,
        WorkItemStatus.WAITING_CLIENT,
        WorkItemStatus.WAITING_INTERNAL,
        WorkItemStatus.WAITING_THIRD_PARTY,
        WorkItemStatus.ESCALATED,
    ):
        assert relay.status_symbol(_Item(status)) == relay.SYMBOL_WORKING, status


def test_every_status_gets_a_symbol() -> None:
    """Structural, so a new status cannot ship without one — the mistake made
    when Compliance and Risk was added as a fifth department."""
    for status in WorkItemStatus:
        assert relay.status_symbol(_Item(status)) in SYMBOLS, status


def test_the_four_symbols_are_distinct() -> None:
    """Two stages sharing a glyph would be worse than no glyph."""
    assert len(SYMBOLS) == 4


# --------------------------------------------------------------------------
# On the title, in the right order
# --------------------------------------------------------------------------

def test_the_light_still_leads() -> None:
    """The topic list truncates from the right. Whatever else goes on the
    title, "is anyone on this" has to be the character that survives."""
    title = relay.topic_name(_Item(WorkItemStatus.OPEN), "Acme Payments")
    assert title.startswith(relay.LIGHT_UNCLAIMED)


def test_the_symbol_sits_beside_the_light() -> None:
    title = relay.topic_name(_Item(WorkItemStatus.OPEN), "Acme Payments")
    assert relay.SYMBOL_UNCLAIMED in title
    assert title.index(relay.LIGHT_UNCLAIMED) < title.index(relay.SYMBOL_UNCLAIMED)
    assert title.index(relay.SYMBOL_UNCLAIMED) < title.index("ACME-1067")


def test_the_priority_mark_still_follows_both() -> None:
    title = relay.topic_name(
        _Item(WorkItemStatus.OPEN, priority=Priority.HIGH), "Acme Payments"
    )
    mark = relay.PRIORITY_MARKS[Priority.HIGH]
    assert title.index(relay.SYMBOL_UNCLAIMED) < title.index(mark)
    assert title.index(mark) < title.index("ACME-1067")


def test_a_closed_request_carries_no_urgency() -> None:
    """Unchanged behaviour, retested because the title changed around it."""
    title = relay.topic_name(
        _Item(WorkItemStatus.CLOSED, priority=Priority.CRITICAL), "Acme Payments"
    )
    assert relay.PRIORITY_MARKS[Priority.CRITICAL] not in title
    assert relay.SYMBOL_CLOSED in title


def test_the_subject_survives_a_long_one() -> None:
    """Telegram caps a topic name at 128. Three leading glyphs now come out of
    the subject, which is the only part that says what the request is about —
    so the cap is worth a test rather than an assumption."""
    long_subject = "settlement missing for the March invoices " * 5
    title = relay.topic_name(
        _Item(WorkItemStatus.OPEN, subject=long_subject), "Acme Payments"
    )
    assert len(title) <= 128
    assert "ACME-1067" in title, "the reference was truncated away"


# --------------------------------------------------------------------------
# And the bubble stays blue
# --------------------------------------------------------------------------

def test_the_topic_is_still_created_blue() -> None:
    """NexterPay's actual decision, 14 September.

    They wanted the symbol *and* the blue. Setting a custom emoji icon would
    have replaced the bubble, so the symbol went on the name instead. If
    somebody later sets `icon_custom_emoji_id`, the blue disappears and this
    test is how they find out it was a choice rather than an oversight.
    """
    from app.services import gateway as gateway_module

    tree = ast.parse(inspect.getsource(gateway_module))

    # Scoped to the real implementation. Three classes declare `create_topic`
    # — the Protocol, the aiogram one and the fake — and `ast.walk` does not
    # promise source order, so an unscoped search found the Protocol's `...`
    # and reported that topics were no longer coloured. The test was wrong, not
    # the code, which is its own small lesson about structural guards.
    implementation = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "AiogramGateway"
        ),
        None,
    )
    assert implementation is not None, "AiogramGateway not found"

    for node in implementation.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_topic":
            call = ast.unparse(node)
            assert "icon_color" in call, "topics are no longer created coloured"
            assert "icon_custom_emoji_id" not in call, (
                "a custom emoji icon replaces the coloured bubble, which "
                "NexterPay asked to keep"
            )
            return
    raise AssertionError("AiogramGateway.create_topic not found")
