"""The bot does not talk back to the desk about what the desk just did.

Jason, 7 October, describing how the FX desk should feel to Slim, who runs it:

    when Slim sends a message normally, nothing happens until they respond,
    for us, lots happens with the bot... the key here is that the bot should
    be more hidden, and only say things on reaction, not continual dialogue
    to Slim. if he sends a message, the bot knows, but without an answer from
    the client, its waiting. as he would when he is waiting for them... not
    AI, just a set of rules.

Four rules, and they are rules rather than taste:

  * the desk acts             -> nothing is said
  * the other side moves      -> say so
  * nobody moves for too long -> say so, which is `/npbook`
  * something failed          -> say so

The fourth is what makes the first safe, and it is the only one worth a test
file of its own. Silence has to mean *it worked*. The moment a send can fail
quietly, every silence the first rule creates becomes ambiguous, and a desk
that cannot tell "sent" from "never left" will go back to wanting a receipt
for everything - which is where this started.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

FX_HANDLERS = pathlib.Path("app/bot/handlers/fx.py")
FX_RELAY = pathlib.Path("app/services/fx_relay.py")

# The relay functions that put something in front of a counterparty. A handler
# calling one of these has done something outside the building.
OUTWARD_SENDS = {
    "send_rate_quote",
    "send_order",
    "send_settlement",
    "notify_rejected",
    "withdraw_order",
}


def _handlers(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            src = ast.dump(dec)
            if "router" in src and ("message" in src or "callback_query" in src):
                found.append(node)
                break
    return found


def _sending_handlers():
    tree = ast.parse(FX_HANDLERS.read_text(encoding="utf-8"))
    out = []
    for fn in _handlers(tree):
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in OUTWARD_SENDS
                and getattr(func.value, "id", "") == "fx_relay"
            ):
                out.append((fn, func.attr))
                break
    return out


def test_there_are_sends_to_check() -> None:
    """A finder that matches nothing passes for ever."""
    assert _sending_handlers(), (
        "no FX handler appears to send anything outward any more - if the "
        "relay was renamed, update OUTWARD_SENDS"
    )


@pytest.mark.parametrize(
    "case", _sending_handlers(), ids=lambda c: f"{c[0].name}->{c[1]}"
)
def test_a_silent_success_has_a_loud_failure(case) -> None:
    """Every outward send sits in a try whose except tells the desk.

    This is the test the quiet rules are built on. If this fails, the right
    fix is to make the failure speak - not to put the confirmation back.
    """
    fn, sent = case

    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if sent not in ast.unparse(node.body):
            continue
        for handler in node.handlers:
            said = ast.unparse(handler)
            if "answer(" in said or "reply(" in said:
                guarded = True
                break
            # Or the failure is carried to something said later. `settle_save`
            # notifies several clients in a loop and reports the ones it could
            # not reach once, at the end, which is better than interrupting
            # the loop - but it only counts if the thing it collects into is
            # actually spoken.
            carried = {
                node.id for node in ast.walk(handler)
                if isinstance(node, ast.Name)
            }
            spoken = {
                name for name in carried
                if any(
                    f"{name}" in ast.unparse(call.args[0])
                    for call in ast.walk(fn)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"answer", "reply"}
                    and call.args
                )
            }
            if spoken:
                guarded = True
                break
        if guarded:
            break

    assert guarded, (
        f"{fn.name}() calls fx_relay.{sent}() and says nothing when it works "
        f"- which is right - but a failure does not reach the desk either. "
        f"Silence then means 'sent' and 'never left' at the same time. Wrap "
        f"the send in try/except and answer with explain(exc)."
    )


# --------------------------------------------------------------------------
# Rule one: the desk acts, nothing is said
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "gone",
    [
        "Sent to the {side.value}.",
        "Sent. The client has Yes and No to tap.",
        "Sent to the client, with a button to confirm receipt.",
        "Rate sent to the client.",
        "Order sent to the {side.value}.",
        "Settlement passed to the client.",
    ],
)
def test_the_old_receipts_are_gone(gone: str) -> None:
    """Each of these fired after the desk pressed Send.

    Two of them fired at once, in fact - one from the handler and one from
    `_announce` - so a single tap produced two messages telling Slim a thing
    he had just done himself.
    """
    source = (
        FX_HANDLERS.read_text(encoding="utf-8")
        + FX_RELAY.read_text(encoding="utf-8")
    )
    # Allowed inside a comment explaining why it went; not as a string we send.
    sending = [
        line for line in source.splitlines()
        if gone in line and not line.strip().startswith("#")
    ]
    assert not sending, f"still saying {gone!r}: {sending}"


def test_announcing_is_only_for_things_the_desk_did_not_do() -> None:
    """`_announce` posts into the deal's topic.

    It is now reached from exactly one place: the function that fires when a
    counterparty answers one of our buttons. Anything else calling it is the
    old habit coming back.
    """
    tree = ast.parse(FX_RELAY.read_text(encoding="utf-8"))

    callers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(fn):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_announce"
            ):
                callers.add(fn.name)

    assert callers == {"announce_counterparty_reply"}, (
        f"_announce is called from {sorted(callers)}. It exists to report "
        f"that the other side moved; calling it after our own send is the "
        f"continual dialogue Jason asked us to remove."
    )


# --------------------------------------------------------------------------
# The same rule, where the desk types into a counterparty's group
# --------------------------------------------------------------------------

def test_the_watcher_ignores_the_desk_itself() -> None:
    """Slim posting a rate to a client is not news to Slim.

    The capture layer read every message in a counterparty group without
    looking at who sent it, so the desk's own messages came straight back as
    observations. "if he sends a message, the bot knows, but without an
    answer from the client, its waiting."
    """
    source = pathlib.Path("app/bot/handlers/capture.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    watch = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "watch"
    )
    body = ast.unparse(watch)

    assert "resolve_staff" in body, (
        "watch() does not check who sent the message, so the desk's own "
        "messages are announced back to the desk"
    )
    # And it must still hand the message on rather than swallow it.
    assert not [n for n in ast.walk(watch) if isinstance(n, ast.Return)]
    assert body.count("raise SkipHandler") >= 4


def test_nothing_treats_the_desk_as_the_counterparty() -> None:
    """The class, across both handlers that read a counterparty's group.

    Found live on 9 October. The capture layer had been taught to ignore the
    desk's own messages, and the observation duly stopped - but the same
    message still arrived in the Operations topic through `client_reply`, as
    "Message received from peter", quoted the way a client's words are quoted
    and with a Reply button under it.

    The noise was the smaller half. `relay_client_message` records INBOUND
    against CLIENT_MESSAGE_RECEIVED, so the ledger said the client had said
    something they never said, and that ledger is what a dispute is settled
    from.

    Two handlers read those groups. Both must know the difference between
    the counterparty and us, and a third added later must too.
    """
    readers = {
        "app/bot/handlers/capture.py": "watch",
        "app/bot/handlers/client.py": "client_reply",
    }

    for path, name in readers.items():
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        fn = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        assert "resolve_staff" in ast.unparse(fn), (
            f"{path}::{name} acts on every message in a counterparty group "
            f"without asking who sent it, so the desk's own messages are "
            f"treated as the counterparty's"
        )


def test_the_watcher_still_speaks_for_a_counterparty() -> None:
    """Rule two is untouched: the other side moving is the whole point."""
    source = pathlib.Path("app/bot/handlers/capture.py").read_text(encoding="utf-8")
    assert "send_message" in source, (
        "the watcher no longer tells the desk anything at all - quiet about "
        "our own actions, not deaf to theirs"
    )
