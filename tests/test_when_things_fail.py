"""What is left behind when a message to a counterparty does not arrive.

Found on 25 October, failing sends on purpose ahead of the November launch.

A handler does two things inside one `session_scope`: it moves the deal, and
it tells the counterparty. The move says something about the world - "awaiting
their confirmation" means *we have asked them* - and the telling is what makes
it true.

When the send failed, the handler caught the error, replied, and returned. A
`return` leaves the `async with` block normally, so `session_scope` committed
on the way out. The deal advanced; the counterparty had nothing in front of
them; and `/npbook` listed it under "waiting on clients". The desk would have
chased somebody who was never asked, from a book that looked entirely right.

Not every failure should roll back, which is why this is not a blanket rule.
`settle_save` records a payment that has already happened in the world and
then tells several clients about it - a group we cannot reach does not unmake
the money, so that one reports the failure and keeps the record. The rule here
is narrower: **a state change that asserts the counterparty has been contacted
must not outlive the contact.**
"""

from __future__ import annotations

import ast
import pathlib

import pytest

HANDLERS = pathlib.Path("app/bot/handlers/fx.py")

# The domain calls that move a deal on the strength of a message arriving.
ASSERTS_CONTACT = {"create_client_order", "create_supplier_order", "record_hash"}


def _handlers_that_move_then_send():
    """Every handler that moves a deal and then tells somebody, in one try.

    The whole function is searched for the domain call, not just the try
    block, because one of them reaches it through a variable:

        create = fx.create_client_order if side is CLIENT else ...
        await create(...)

    The first version of this looked only for `fx.create_client_order(` and
    therefore found `settle_send` and missed `send_order` - which is the
    handler the fault was actually found in. A guard that silently covers
    less than it claims is worse than no guard, because it reads like
    coverage.
    """
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        whole = ast.unparse(node)
        if not any(f"fx.{name}" in whole for name in ASSERTS_CONTACT):
            continue
        for block in ast.walk(node):
            if isinstance(block, ast.Try) and "fx_relay.send_" in ast.unparse(block.body):
                found.append((node.name, block))
    return found


def test_there_is_something_to_check() -> None:
    assert _handlers_that_move_then_send(), (
        "no handler moves a deal and then sends in the same try - if the "
        "shape changed, point ASSERTS_CONTACT at wherever it went"
    )


@pytest.mark.parametrize(
    "case", _handlers_that_move_then_send(), ids=lambda c: c[0]
)
def test_a_failed_send_does_not_leave_the_deal_advanced(case) -> None:
    name, block = case
    handlers = ast.unparse(block.handlers)

    assert "rollback" in handlers, (
        f"{name}() moves the deal and then tells the counterparty. If the "
        f"send fails it returns, the session commits on the way out, and the "
        f"deal is left claiming they were asked when they were not. Roll back "
        f"before replying."
    )


@pytest.mark.parametrize(
    "case", _handlers_that_move_then_send(), ids=lambda c: c[0]
)
def test_the_desk_is_told_the_deal_did_not_move(case) -> None:
    """Rolling back silently would be its own trap: the desk would assume the
    deal had advanced and not try again."""
    name, block = case
    handlers = ast.unparse(block.handlers)

    assert "Nothing has changed" in handlers, (
        f"{name}() rolls back without saying so, so the desk cannot tell "
        f"whether to retry"
    )
    assert "answer(" in handlers or "reply(" in handlers


def test_a_settlement_already_in_the_world_is_not_rolled_back() -> None:
    """The counter-example, named so the rule above cannot spread.

    `settle_save` records a payment the supplier has already made. A client
    group we cannot reach is a notice that failed, not a payment that did -
    rolling the record back would lose the one fact that was true.
    """
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "settle_save"
    )
    body = ast.unparse(fn)

    assert "Could not reach the group" in body, (
        "settle_save no longer reports the clients it could not tell"
    )
    assert "failed.append" in body or "failed" in body


# --------------------------------------------------------------------------
# A picker that offers what the step cannot do
# --------------------------------------------------------------------------
#
# Found live on 25 October, driving the bot. `/npordercl` listed every open
# deal, including one still at Rate requested. Choosing it asked four
# questions, produced a preview headed "This will be sent to Acme Payments"
# with a live Send button, and only refused after the tap.
#
# `_open_deals` has taken an `allowed` filter since it was written, with a
# comment saying why: "A picker that offers a deal and then says no is how
# somebody learns the state machine by trial and error in front of a client."
# The two order steps never passed it.

def test_the_order_pickers_offer_only_what_they_can_act_on() -> None:
    from app.bot.handlers.fx import ORDERABLE
    from app.domain.enums import FxOrderStatus, FxSide

    assert ORDERABLE[FxSide.CLIENT] == (
        FxOrderStatus.RATE_QUOTED,
        FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
    )
    assert ORDERABLE[FxSide.SUPPLIER] == (
        FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
    )


def test_the_picker_agrees_with_the_rule_that_refuses() -> None:
    """The list the desk is offered is taken from the same states the domain
    requires, so the two cannot drift apart."""
    import inspect

    from app.bot.handlers.fx import ORDERABLE
    from app.domain import fx as domain
    from app.domain.enums import FxSide

    for side, creator in (
        (FxSide.CLIENT, domain.create_client_order),
        (FxSide.SUPPLIER, domain.create_supplier_order),
    ):
        source = inspect.getsource(creator)
        for status in ORDERABLE[side]:
            assert status.name in source, (
                f"the {side.value} picker offers {status.name}, which "
                f"{creator.__name__} does not accept"
            )


def test_the_refusal_does_not_guess_why_the_send_failed() -> None:
    """Found in the same session, and it was mine.

    The rollback message said "Try again when that group is reachable",
    which is one possible cause. The same path is taken when the deal was
    simply in the wrong state, and the desk was sent looking for a Telegram
    problem that did not exist. `explain(exc)` already says why; the rollback
    half says only what it left behind.

    Checked against the strings the code can actually send, not the file's
    text. The first version of this grepped the source and failed on the
    comment above, which quotes the old wording in order to explain it -
    source text is not code, a lesson this project has paid for before.
    """
    tree = ast.parse(HANDLERS.read_text(encoding="utf-8"))
    spoken = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert not [text for text in spoken if "reachable" in text], (
        "a refusal still guesses that the group was unreachable"
    )
    assert len([text for text in spoken if "Nothing has changed" in text]) == 2


# --------------------------------------------------------------------------
# A refusal the counterparty can read
# --------------------------------------------------------------------------

async def test_the_wrong_room_refusal_explains_nothing_internal(
    session, acme_support
) -> None:
    """Running a staff command in a client's group answers *in that group*,
    where the client reads it too.

    It used to go on to describe "your Operations Group - the internal one
    where the request topics are", explaining NexterPay's internal filing to
    an outsider in order to tell a member of staff something they already
    know. Found on 25 October, running /npgroups in a client group on purpose.

    It still speaks: a staff member whose command did nothing would otherwise
    assume it had worked.
    """
    from app.bot.deps import refusal_reason

    text = await refusal_reason(5001, session, acme_support.telegram_chat_id)

    assert text, "a staff command in the wrong room now says nothing at all"
    for internal in ("Operations Group", "topic", "internal", "request topics"):
        assert internal.lower() not in text.lower(), (
            f"the client can read this, and it mentions {internal!r}"
        )
