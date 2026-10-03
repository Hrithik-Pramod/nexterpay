"""Every way out of NexterPay, checked as a class rather than one at a time.

This file exists because of a pattern, not because of a bug.

Four times now, the same fault has been fixed where it was *noticed* instead
of where it *lives*, and each time the remaining instances stayed in
production until somebody tripped over them:

  * the client's reference leaked to a counterparty - found three times, in
    three different functions, over three separate rounds;
  * a request raised internally reached the counterparty - found three times:
    the closure notice, `/npreply`, and the client's own My Requests list, the
    last only by querying production records;
  * a draft that was still live while its work ran, so a second tap sent twice
    - fixed in `outbound.py` and `staff.py` on 29 September, found again in
    `tell_client_the_rate` on 3 October, and again in `send_order` the same
    day by the script that became this file;
  * a tagged message sent as plain text, because `parse_mode` was set inside
    one branch and not at the top.

Every one of those was invisible to a suite of 800-odd passing tests, because
each test knew about one function. The tests were right and the shape was
wrong: a guard written per-instance protects the instances that existed when
it was written.

So these tests do not name handlers. They *enumerate* them - every callback
handler that reaches an outward writer - and assert the properties that must
hold for all of them. A new door is covered the day it is added, and a new
door that forgets the pattern fails this file rather than a client's group.

The failure message in each case names the offender and the fix, because the
person who sees it will usually not be the person who wrote this.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

HANDLERS = pathlib.Path("app/bot/handlers")

# The functions that actually put a message in front of a counterparty.
#
# Derived rather than guessed: these are the functions in relay.py and
# fx_relay.py whose bodies reach a counterparty chat id. `test_the_writer_list
# _is_still_complete` below recomputes that and fails if the set has changed,
# so this list cannot quietly go stale the way a hand-maintained one would.
OUTWARD_WRITERS = {
    "open_request",
    "relay_client_message",
    "send_client_reply",
    "open_internal",
    "answer_internal",
    "open_outbound",
    "notify_owner",
    "close",
    "send_rate_quote",
    "send_order",
    "send_settlement",
    "notify_rejected",
}

# Ways a handler can claim the work before doing it. Any one of them is
# enough; what matters is that it happens first.
CLAIMS = ("clear", "edit_reply_markup", "_clear_buttons")


def _callback_handlers() -> list[tuple[str, ast.AsyncFunctionDef]]:
    found = []
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            decorators = " ".join(ast.dump(d) for d in node.decorator_list)
            if "callback_query" in decorators:
                found.append((path.name, node))
    return found


def _called_name(node: ast.Call) -> str:
    """The bare name of whatever is being called.

    `relay.open_outbound(...)` and `open_outbound(...)` are the same door, and
    which one a module happens to write is not a property worth testing.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _outward_calls(fn: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Calls inside this handler that put something in front of a counterparty.

    Walked as AST rather than matched as text, which matters more than it
    sounds: the first version of this file compared string positions, and
    `send_order` passed by accident because its own `def send_order(` line
    matched the writer it calls. A test that can be fooled by the name of the
    function it is checking is not a guard.
    """
    return [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _called_name(node) in OUTWARD_WRITERS
    ]


def _claim_lines(fn: ast.AsyncFunctionDef) -> list[int]:
    """Lines where this handler unconditionally claims the work.

    Only statements at the top level of the function count, and that
    restriction is the entire value of this helper.

    `send_order` has an `await state.clear()` near the top - inside the branch
    that handles an expired draft and then returns. It claims nothing on the
    path that sends, because that path never runs it. A check that merely
    asked "is there a claim before the send" was satisfied by it, which is how
    the first version of this file passed over the live bug it had just been
    written to catch. The guard has to ask whether the claim is on the way
    through, not whether one exists somewhere above.

    A claim buried in a conditional is therefore not counted even when it is
    genuinely on the path. That is deliberate: the shape this codebase uses -
    and the shape that is easy to verify by eye - is an unconditional claim
    before the work starts.
    """
    lines = []
    for stmt in fn.body:
        # The statement itself must be the claim - `await state.clear()` on
        # its own line, at the top level of the function. Anything nested is
        # either conditional or inside the session block, and both were found
        # giving false passes: `send_order` has a `state.clear()` inside its
        # `async with`, several lines below the send, and attributing it to
        # the line the `async with` opens on made it look like a claim made
        # before the work.
        if not isinstance(stmt, ast.Expr):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _called_name(node) in CLAIMS:
                lines.append(node.lineno)
    return lines


def _doors() -> list[tuple[str, ast.AsyncFunctionDef]]:
    """Callback handlers that reach an outward writer."""
    return [
        (name, node) for name, node in _callback_handlers()
        if _outward_calls(node)
    ]


def _ids(case) -> str:
    return f"{case[0]}::{case[1].name}"


# --------------------------------------------------------------------------
# There are doors, and the list is live
# --------------------------------------------------------------------------

def test_there_are_doors_to_check() -> None:
    """The guard below is a loop. A loop over nothing passes silently, which
    is the one way a test like this fails open - a rename of a decorator or a
    move of the handlers package would empty it and nobody would notice."""
    doors = _doors()
    assert len(doors) >= 6, (
        f"only {len(doors)} outward doors found. Either the handlers moved, "
        f"or the way they are decorated changed, and this file is now "
        f"guarding nothing."
    )


def test_the_writer_list_is_still_complete() -> None:
    """The set above is derived from the services, so it must be rederived.

    A new function in relay.py or fx_relay.py that writes to a counterparty is
    a new way out. If it is not in `OUTWARD_WRITERS`, every handler that calls
    it is invisible to this whole file - which is exactly the failure mode
    this file was written to end.
    """
    found = set()
    for module in ("app/services/relay.py", "app/services/fx_relay.py"):
        tree = ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body = ast.unparse(node)
            if "gateway.send_message" in body or "counterparty.telegram_chat_id" in body:
                found.add(node.name)

    # Private helpers are the plumbing the public ones share, not doors of
    # their own. `announce`, `claim`, `post_anchor` and `_reopen_from_archive`
    # write into Operations, not outward.
    internal = {
        "announce", "claim", "post_anchor", "_reopen_from_archive", "_announce",
    }
    missing = found - internal - OUTWARD_WRITERS
    assert not missing, (
        f"new ways out of NexterPay that this file does not know about: "
        f"{sorted(missing)}. Add them to OUTWARD_WRITERS - and give them the "
        f"same scrutiny the others had, because every handler that calls them "
        f"is now in scope for the tests below."
    )


# --------------------------------------------------------------------------
# The property, held for all of them
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", _doors(), ids=_ids)
def test_every_door_claims_its_work_before_doing_it(case) -> None:
    """Claim first, then send. Never the other way round.

    A handler that sends and *then* clears its draft is open for the whole
    duration of the send - which is precisely the window in which somebody
    taps again, because nothing has visibly happened yet. That is not a
    theoretical race: it produced two identical prices in a client's group on
    3 October, and before that two "we will send the order through shortly"
    a minute apart, which is how NexterPay found it.

    Checked on source order rather than by running the handler, because the
    bug is the ordering of two statements and that is exactly what source
    order is.
    """
    filename, node = case

    writes_at = min(call.lineno for call in _outward_calls(node))
    claims_at = _claim_lines(node)

    assert claims_at, (
        f"{filename}::{node.name} sends to a counterparty but never claims "
        f"the work. Clear the FSM state or strip the buttons before sending, "
        f"or a second tap sends it again."
    )
    assert min(claims_at) < writes_at, (
        f"{filename}::{node.name} sends to a counterparty on line {writes_at} "
        f"but does not claim the work until line {min(claims_at)}, so a second "
        f"tap during the send goes through too. Move the state.clear() / "
        f"edit_reply_markup() / _clear_buttons() call above the send - see "
        f"tell_client_the_rate in fx.py for the shape."
    )


@pytest.mark.parametrize("case", _doors(), ids=_ids)
def test_every_door_speaks_when_it_refuses(case) -> None:
    """A door that closes in silence is indistinguishable from a crash.

    The same argument as test_silent_refusals, applied to this narrower set
    because the stakes are higher here: somebody who taps Send and sees
    nothing happen taps Send again, and these are the handlers where that
    costs a message to a counterparty.
    """
    filename, node = case
    body = ast.unparse(node)
    assert "answer(" in body or "reply(" in body, (
        f"{filename}::{node.name} can finish without saying anything. "
        f"Somebody who taps and sees nothing taps again."
    )


@pytest.mark.parametrize("case", _doors(), ids=_ids)
def test_no_door_composes_an_internal_reference(case) -> None:
    """`display_reference` carries both counterparties' codes.

    It is correct in an Operations Group and a leak anywhere else: a supplier
    who can see the client code learns who they are quoting for, and a client
    who can see the supplier code learns where the price came from. Either is
    most of the margin.

    The handlers must not build counterparty text themselves - the relay
    functions compose it from one side's columns through `view_for` and
    `reference_for`, which is what makes a leak need a wrong call rather than
    a forgetful one. A handler reaching for `display_reference` on the way to
    a send is the shape the three reference leaks all had.

    Checked as "is it an argument to the send", not as "does it appear
    earlier in the function". Naming the deal in the Operations Group is the
    normal and correct use - `tell_client_the_rate` does it in the refusal it
    prints when a rate has already gone out, and that message never leaves
    the Operations Group. A positional test would have failed that and taught
    somebody to delete a perfectly good line.
    """
    filename, node = case

    for call in _outward_calls(node):
        passed = {
            inner.attr for inner in ast.walk(call)
            if isinstance(inner, ast.Attribute)
        }
        assert "display_reference" not in passed, (
            f"{filename}::{node.name} passes display_reference into "
            f"{_called_name(call)}() on line {call.lineno}. That reference "
            f"carries both counterparties' codes. Let the relay compose what "
            f"the counterparty sees - client_reference and supplier_reference "
            f"exist for exactly this."
        )
