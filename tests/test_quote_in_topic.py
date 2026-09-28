"""`/npquote` inside a deal's own topic does not ask which deal.

NexterPay, 28 September: "if you run /npquote inside a deal's own topic it
still asks which deal, when it could just know... Yes do that."

The picker was shown wherever the command was typed, including in the one
place that already answers the question. Standing in FXACME-1002's topic and
being asked which of two deals you meant is the kind of small friction that
makes a flow feel longer than it is — Jason hit it while testing a quote and
read the picker as the wrong screen.

Two things are worth guarding rather than the happy path alone.

**It must not guess.** A topic with two quotable deals against it gets the
picker, because choosing between two prices on somebody's behalf silently is
worse than one extra tap. The shortcut exists to skip a question with only one
possible answer, not to answer an open one.

**Both routes must stay the same flow.** Everything after the deal is
identified runs through `_begin_quote`, so the shortcut cannot become a second,
quietly different version of quoting. That is asserted on the source, because
it is a structural property rather than a behavioural one.
"""

from __future__ import annotations

import pathlib

import pytest

from app.bot.handlers import fx as handlers
from app.db.models import Client
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor


async def _deal_on_a_topic(session, acme_support, support_ops, operator, thread_id):
    """A client request with a topic, and a deal opened against it."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="EUR to USDT",
        original_message="What rate can you do for 250k EUR?",
        raised_by_name="Tom Baker",
    )
    await wi.attach_topic(session, item, thread_id)
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    return item, order


async def test_the_deal_is_found_from_the_topic(
    session, acme_support, support_ops, operator
):
    item, order = await _deal_on_a_topic(
        session, acme_support, support_ops, operator, thread_id=4001
    )
    assert order.status in handlers.QUOTABLE

    found = await handlers._deal_in_this_topic(session, support_ops, 4001)
    assert found == order.id
    assert item.topic_id == 4001


async def test_a_topic_with_no_deal_falls_back_to_the_picker(
    session, acme_support, support_ops, operator
):
    """An ordinary request's topic. Nothing to shortcut, so nothing is."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="Settlement missing",
        original_message="the 14:02 payment never arrived",
        raised_by_name="Haze",
    )
    await wi.attach_topic(session, item, 4002)

    assert await handlers._deal_in_this_topic(session, support_ops, 4002) is None


async def test_the_general_topic_falls_back_to_the_picker(
    session, acme_support, support_ops, operator
):
    """Where Jason ran it. No thread, no deal, and the picker is correct."""
    await _deal_on_a_topic(session, acme_support, support_ops, operator, thread_id=4003)

    assert await handlers._deal_in_this_topic(session, support_ops, None) is None


async def test_two_deals_on_one_topic_still_ask(
    session, acme_support, support_ops, operator
):
    """The case the shortcut must refuse.

    Guessing which of two prices somebody meant to set, silently, is worse
    than one extra tap. The shortcut skips a question with a single possible
    answer; it does not answer an open one.
    """
    item, first = await _deal_on_a_topic(
        session, acme_support, support_ops, operator, thread_id=4004
    )
    client = await session.get(Client, item.client_id)
    second = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    assert first.id != second.id

    assert await handlers._deal_in_this_topic(session, support_ops, 4004) is None


async def test_a_closed_deal_is_not_picked_up(
    session, acme_support, support_ops, operator
):
    """Only deals a quote can act on. A finished deal sharing the topic must
    not be offered, or the shortcut would start a price on settled work."""
    item, order = await _deal_on_a_topic(
        session, acme_support, support_ops, operator, thread_id=4005
    )
    order.status = FxOrderStatus.CLOSED
    await session.flush()

    assert await handlers._deal_in_this_topic(session, support_ops, 4005) is None


# --------------------------------------------------------------------------
# One flow, two ways in
# --------------------------------------------------------------------------

def test_both_routes_run_the_same_step() -> None:
    """The shortcut must not become a second version of quoting.

    Checked on the source: the picker's handler and the command both call
    `_begin_quote` and neither reimplements what follows. If one of them ever
    grows its own copy of the fork, this fails — which is the point, because
    the copy would be invisible until the two behaved differently.
    """
    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")

    picker = source[source.index("async def quote_pick_deal"):]
    picker = picker[: picker.index("@router.callback_query(F.data.startswith(\"fx:qsup:\"))")]

    command = source[source.index("async def quote("):]
    command = command[: command.index("async def _deal_in_this_topic")]

    for name, body in (("picker", picker), ("command", command)):
        assert "_begin_quote(" in body, f"{name} does not go through _begin_quote"
        assert "awaiting_client_rate" not in body, (
            f"{name} sets the quote state itself instead of letting "
            f"_begin_quote do it"
        )


@pytest.mark.parametrize("name", ["_deal_in_this_topic", "_begin_quote"])
def test_the_helpers_exist_under_the_names_the_tests_use(name: str) -> None:
    """These are private, and a rename that left the tests importing the old
    name would fail confusingly rather than clearly."""
    assert hasattr(handlers, name)


# --------------------------------------------------------------------------
# Earning the silent-refusal exemption
#
# `quote()` now ends by handing off to `_begin_quote` and returning, which
# `test_silent_refusals` read as a handler giving up without a word. It is
# not - the work moved one function along - but the guard cannot see through
# the indirection, and widening it to assume helpers speak would blunt the
# thing that has caught real silence twice.
#
# So `_begin_quote` is on SPEAKING_HELPERS and this is the test that earns it,
# the same arrangement `_ask_reason` has. If a silent path is ever added, this
# fails rather than the exemption quietly becoming untrue.
# --------------------------------------------------------------------------

def test_the_quote_step_always_speaks() -> None:
    """Every return in `_begin_quote` is immediately preceded by a reply.

    Checked as "the statement before it answers" rather than by walking paths,
    because that is the shape the function is written in - answer, return -
    and a looser check would pass on a version that is not.
    """
    import ast

    tree = ast.parse(pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "_begin_quote"
    )

    def answers(stmt) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "answer"
            for node in ast.walk(stmt)
        )

    silent = []

    def walk(body) -> None:
        previous = None
        for stmt in body:
            if isinstance(stmt, ast.Return) and not (previous and answers(previous)):
                silent.append(stmt.lineno)
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if inner:
                    walk(inner)
            previous = stmt

    walk(fn.body)

    assert not silent, (
        f"_begin_quote returns without replying at line(s) {silent}. It is "
        f"listed in SPEAKING_HELPERS in test_silent_refusals.py on the promise "
        f"that it always answers — either keep that true or take it off the list."
    )


def test_the_exemption_is_actually_registered() -> None:
    """The two halves have to stay together: the promise in one file and the
    proof in another are only worth something as a pair."""
    listed = pathlib.Path("tests/test_silent_refusals.py").read_text(encoding="utf-8")
    assert '"_begin_quote"' in listed
