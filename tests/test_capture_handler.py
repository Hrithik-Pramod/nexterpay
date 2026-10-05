"""The capture layer, where it meets Telegram.

What it understands is tested in `test_capture.py`, against their real
messages and without a database. This covers the three things the handler
must never do, each of which is obvious until somebody changes it.

Jason, 4 October: "think working along side him", and that this should be the
primary way the FX desk works. Primary and automatic are different things,
and the distance between them is these three rules.
"""

from __future__ import annotations

import ast
import pathlib

SOURCE = pathlib.Path("app/bot/handlers/capture.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name: str) -> ast.AsyncFunctionDef:
    return next(
        node for node in ast.walk(TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


# --------------------------------------------------------------------------
# It never speaks where the conversation is
# --------------------------------------------------------------------------

def test_the_watcher_never_answers_in_the_counterparty_group() -> None:
    """Not a confirmation, not a tick.

    The desk is mid-conversation with a client or a supplier, and the platform
    joining in is the thing being designed out. Everything it has to say goes
    to the Operations Group.
    """
    watch = ast.unparse(_function("watch"))

    for speaking in ("message.reply(", "message.answer("):
        assert speaking not in watch, (
            f"watch() calls {speaking} - that lands in the counterparty's "
            f"group, which is the one place this must stay silent."
        )


def test_everything_it_sends_goes_to_operations() -> None:
    """The only send in the watcher is addressed to the Operations chat id."""
    watch = _function("watch")

    sends = [
        node for node in ast.walk(watch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
    ]
    assert sends, "the watcher no longer tells the desk anything"

    for call in sends:
        first = ast.unparse(call.args[0]) if call.args else ""
        assert "ops" in first.lower(), (
            f"a send addressed to {first!r} rather than the Operations Group"
        )


# --------------------------------------------------------------------------
# It never swallows a message
# --------------------------------------------------------------------------

def test_every_path_hands_the_message_on() -> None:
    """This sits in front of the real handlers, and is not a replacement.

    A `return` anywhere in the watcher consumes the update before the client
    router sees it - which is the silent-loss failure this project has already
    paid for once, when a client's reply never reached the desk.
    """
    watch = _function("watch")

    returns = [
        node for node in ast.walk(watch)
        if isinstance(node, ast.Return)
    ]
    assert not returns, (
        "watch() returns somewhere. Every path must raise SkipHandler so the "
        "existing routing still runs."
    )
    assert ast.unparse(watch).count("raise SkipHandler") >= 3


def test_it_is_registered_in_front_of_the_client_router() -> None:
    """Order is the only thing that makes it an observer."""
    main = pathlib.Path("app/bot/main.py").read_text(encoding="utf-8")

    assert main.index("include_router(capture.router)") < main.index(
        "include_router(client.router)"
    )


# --------------------------------------------------------------------------
# It never records anything
# --------------------------------------------------------------------------

def test_the_watcher_writes_nothing() -> None:
    """Primary is not automatic. The desk says yes."""
    watch = ast.unparse(_function("watch"))

    for writing in ("settlement.record(", "session.add(", "commit("):
        assert writing not in watch


def test_recording_goes_through_the_flow_the_desk_already_uses() -> None:
    """Deliberately the same path as `/npsettle` rather than a shortcut.

    The preview, the matching, the discrepancy check and the refusal to choose
    between two deals are the value of that flow. A capture path with its own
    copy would be a second, quieter way to record a payment - which is the
    shape of every expensive fault on this project.
    """
    record = ast.unparse(_function("record"))

    assert "settle_capture_block_from(" in record
    assert "settlement.record(" not in record


def test_the_record_button_claims_before_it_acts() -> None:
    """Same rule as every other button here."""
    record = ast.unparse(_function("record"))

    assert record.index("edit_reply_markup") < record.index(
        "settle_capture_block_from("
    )


def test_there_is_a_way_to_say_no() -> None:
    """A platform that only offers yes teaches people to leave prompts
    sitting there, and a column of unanswered prompts is how somebody stops
    reading them."""
    assert "cap:ignore" in SOURCE
    assert any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == "ignore"
        for node in ast.walk(TREE)
    )


# --------------------------------------------------------------------------
# What it says
# --------------------------------------------------------------------------

def test_the_desk_is_told_which_group_it_came_from():
    """They run several conversations at once, and "a settlement arrived" is
    not useful without "from whom"."""
    from app.bot.handlers.capture import observation_text
    from app.services.capture import Observation

    text = observation_text(
        "TEST — Pexi Finance",
        [Observation(kind="settlement", summary="2 settlement lines, 39,309.469 USDT")],
    )

    assert "Pexi Finance" in text
    assert "2 settlement lines" in text


def test_it_says_plainly_that_nothing_has_happened():
    from app.bot.handlers.capture import observation_text
    from app.services.capture import Observation

    text = observation_text("x", [Observation(kind="rate", summary="a rate")])
    assert "nothing has been recorded" in text.lower()
