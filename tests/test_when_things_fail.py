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
