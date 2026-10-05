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


# --------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------
#
# Found live on 5 October, the first time anybody tapped Record: the flow
# answered "You are not registered as staff." to a registered member of staff.
#
# A callback splits two things a message normally carries together - where to
# reply, and who is asking. `query.message` is the **bot's own** post, so its
# `from_user` is the bot; the person is `query.from_user`. Handing the bot's
# message to a flow that works out identity from `message.from_user` asks the
# platform whether the bot is staff, and it is not.
#
# Worth a test rather than a fix, because the failure was indistinguishable
# from a permissions problem and would have had the desk editing staff records
# to chase it.

class _Person:
    id = 4242
    is_bot = False


class _Bot:
    id = 99
    is_bot = True


class _Post:
    """The bot's own observation message - the one carrying the buttons."""

    from_user = _Bot()

    def __init__(self) -> None:
        self.chat = type("c", (), {"id": -100})()
        self.message_id = 7
        self.said: list[str] = []

    async def edit_reply_markup(self, **_kw) -> None:
        return None

    async def answer(self, text, **_kw):
        self.said.append(text)
        return None


class _Query:
    def __init__(self) -> None:
        self.message = _Post()
        self.from_user = _Person()

    async def answer(self, *_a, **_kw) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.state = None

    async def clear(self) -> None:
        self.state = None

    async def set_state(self, value) -> None:
        self.state = value


async def test_the_person_who_tapped_is_who_the_flow_asks_about(monkeypatch) -> None:
    """Not the bot whose message the button is attached to."""
    from app.bot.handlers import capture as handler

    seen: dict = {}

    async def fake_flow(message, state, pasted, actor=None):
        seen["actor"] = actor
        seen["pasted"] = pasted

    monkeypatch.setattr(handler, "settle_capture_block_from", fake_flow)

    query = _Query()
    handler._PENDING[(query.message.chat.id, query.message.message_id)] = "XOF: 1/2"

    await handler.record(query, _State())

    assert seen.get("actor") is not None, (
        "the settlement flow was given no actor, so it falls back to the "
        "bot's own message and refuses a real member of staff"
    )
    assert seen["actor"].id == _Person.id
    assert not getattr(seen["actor"], "is_bot", False)


def test_the_settlement_flow_can_be_told_who_is_asking() -> None:
    """The other half of the same fault, read off the flow itself.

    `settle_capture_block_from` is called from two places: a message the
    person typed, where `message.from_user` is right, and a button, where it
    is the bot. It therefore has to accept an actor.
    """
    flow = ast.parse(
        pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")
    )
    node = next(
        n for n in ast.walk(flow)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "settle_capture_block_from"
    )

    names = [a.arg for a in node.args.args] + [
        a.arg for a in node.args.kwonlyargs
    ]
    assert "actor" in names, (
        "settle_capture_block_from works out who is asking from the message "
        "it was handed. From a button that message is the bot's."
    )


def test_no_callback_handler_takes_identity_from_the_bots_own_message() -> None:
    """The class, across every handler module.

    `query.message.from_user` is the author of the message the button sits on,
    which for every button this platform sends is the bot. Identity comes from
    `query.from_user`.
    """
    offenders: list[str] = []

    for path in sorted(pathlib.Path("app/bot/handlers").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body = ast.unparse(node)
            if "query.message.from_user" in body:
                offenders.append(f"{path.name}:{node.name}")

    assert not offenders, (
        "these read the author of the bot's own message as if it were the "
        f"person who tapped: {offenders}"
    )
