"""The reset script, checked for the things that cannot be undone.

NexterPay asked for a way to empty the platform at the end of testing: every
ticket wiped, the numbering back to the start, and — added on 20 September —
every topic gone from the Operations Groups and the archive too.

There is no test here that runs it. Deleting every topic in every group is not
something to exercise against a fake and call covered, and the parts worth
guarding are properties of the script's shape rather than of its output.

**The one that matters is the order.** The Bot API has no method that lists the
topics in a group — `deleteForumTopic` exists and nothing enumerates them. The
only record of which topics exist is `work_items.topic_id` and
`archive_topic_id`. So if the rows are deleted first, every topic in every
group becomes permanently unreachable: still sitting there, still full of
client conversations, with nothing left that knows their ids. Somebody would
have to clear them by hand, one at a time, for ever.

Topics, then rows. That is what the first test is for.
"""

from __future__ import annotations

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "reset_environment.py"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_topics_are_deleted_before_the_rows() -> None:
    """The irreversible ordering, asserted on the source.

    If this ever fails, the script will run to completion, report success, and
    leave every topic on the platform orphaned.
    """
    source = _source()
    topics_at = source.index("await gateway.delete_topic(")
    rows_at = source.index("await session.execute(delete(model))")

    assert topics_at < rows_at, (
        "the database is being wiped before the topics are deleted. "
        "After the wipe nothing knows the thread ids, so every topic in every "
        "group is unreachable and has to be cleared by hand."
    )


def test_the_topic_list_is_read_before_anything_is_deleted() -> None:
    """Same reasoning, one step earlier: the ids have to be in memory before
    the first destructive call."""
    source = _source()
    read_at = source.index("topics = await _topics_to_delete(session)")
    first_delete = min(
        source.index("await gateway.delete_topic("),
        source.index("await session.execute(delete(model))"),
    )
    assert read_at < first_delete


def test_a_dry_run_is_the_default() -> None:
    """Running it with no arguments must change nothing. Somebody will run it
    with no arguments to see what it does — that is what people do with an
    unfamiliar script, and it is the right instinct."""
    source = _source()
    assert 'help=\'The phrase "ERASE <database name>"' in source
    assert "if not live:" in source
    assert "Nothing was changed." in source


def test_the_confirmation_names_the_database() -> None:
    """So that running it against production requires typing production's name.

    A --force flag would be one keystroke away from the wrong environment; a
    phrase that has to name the target is not.
    """
    source = _source()
    assert 'expected = f"ERASE {_database_name()}"' in source
    assert "confirm == expected" in source


def test_the_wipe_list_is_written_out_not_reflected() -> None:
    """A list built from the metadata grows silently when somebody adds a
    table, and silent growth is the last property you want in the thing that
    deletes everything."""
    source = _source()
    assert "WIPE_IN_ORDER = [" in source
    assert "metadata.sorted_tables" not in source
    assert "Base.metadata" not in source


def test_children_are_wiped_before_their_parents() -> None:
    """Otherwise the foreign keys refuse and the script dies halfway, which is
    the worst possible moment: topics already gone, rows still there."""
    source = _source()
    block = re.search(r"WIPE_IN_ORDER = \[(.*?)\]", source, re.S).group(1)
    order = [line.strip().rstrip(",") for line in block.strip().splitlines()]

    for child, parent in (
        ("Attachment", "Message"),
        ("Message", "WorkItem"),
        ("Event", "WorkItem"),
        ("WorkItemLink", "WorkItem"),
        ("BroadcastDelivery", "Broadcast"),
        ("FxOrder", "WorkItem"),
    ):
        assert order.index(child) < order.index(parent), (
            f"{child} must be deleted before {parent}"
        )


def test_what_survives_is_what_they_asked_to_survive() -> None:
    """NexterPay, 20 September: keep the groups, keep the staff and their
    levels. Counterparties only go when asked for explicitly."""
    source = _source()
    block = re.search(r"WIPE_IN_ORDER = \[(.*?)\]", source, re.S).group(1)

    for kept in ("Chat", "Staff", "StaffDepartment", "GroupLead"):
        assert kept not in block, f"{kept} must not be wiped"

    # Clients are removable, but only behind their own flag.
    assert "Client" not in block
    assert "if counterparties:" in source
    assert "delete(Client)" in source


def test_the_counters_go_back_to_the_start() -> None:
    """"Reset the clocks", in their words — the next request reads #1000
    again rather than carrying on from wherever testing finished."""
    source = _source()
    assert "COUNTER_START = 1000" in source
    for counter in ("ReferenceCounter", "FxReferenceCounter"):
        assert counter in source


def test_it_says_what_it_cannot_do() -> None:
    """The General topic cannot be deleted by anyone, and messages sitting in
    a client group are not topics and will not be touched. Both will be
    noticed afterwards by whoever expected a clean slate, so both are written
    down at the top rather than discovered."""
    source = _source()
    # Whitespace-normalised: these sentences are wrapped in the docstring, and
    # a test that breaks when somebody reflows a paragraph is a test that
    # teaches people to stop reflowing paragraphs.
    doc = " ".join(source[: source.index('"""', 3)].split())

    assert "General topic cannot be deleted by anybody" in doc
    assert "ordinary messages in an ordinary group, not topics" in doc
