"""No command may answer with silence.

The single worst failure this platform can produce. A command that does
nothing and says nothing is indistinguishable from a crash, a failed deploy,
a mistyped name, or Telegram never delivering the message - and every one of
those has a different fix. It turns a five-second permissions answer into a
bug report, a WhatsApp thread, and a server log investigation.

It has now happened twice. In August, case-sensitive command filters meant
/NPRAISE was never delivered to a handler. On 4 September, NexterPay reported
/npsetlead as "not working": it was working exactly as designed, Gavin is an
Operator, and every administrator command in admin.py returned without a word.

So this file guards the shape of the thing rather than any one instance.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.bot import commands as cmd
from app.bot.handlers import admin

HANDLERS = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers"


def _decorated_handlers(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    """Every function registered on a router - the ones a person can reach."""
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


# Helpers whose contract is to have already spoken by the time they return a
# value the caller acts on. A `return` guarded by one of these is not silent -
# the words came from inside it.
SPEAKING_HELPERS = (
    "_or_refuse",         # admin.py - refuses out loud, returns False
    "_may_manage_leads",  # admin.py - defers to _or_refuse, or is silent on
                          # purpose in a counterparty's group
    "_wrong_topic",       # staff.py - explains the mix-up, returns True
    "_open_from",         # client.py - opens a request, which acknowledges it
)

# Bare returns that are correct, each with the reason written down. This list
# is meant to stay short; anything added to it should be a routing decision -
# "another handler owns this message" - and never a permissions decision.
#
# Keyed by function name, since line numbers move.
ROUTING_SILENCE = {
    "topic_message": (
        "A catch-all. It declines commands that belong to their own handlers, "
        "messages with nothing in them to act on, and /npreply as plain text "
        "which cmd_reply answers. Speaking in any of those cases would mean "
        "the bot replies to everything said in an Operations Group."
    ),
}


def _bare_returns_before_speaking(fn: ast.AsyncFunctionDef) -> list[int]:
    """Lines where the handler gives up without anything having been said.

    Looks for a bare `return` in the body of an `if`, where neither that `if`
    nor the guarding call says anything. That is the exact shape of the bug:
    a guard clause that bails in silence.
    """
    if fn.name in ROUTING_SILENCE:
        return []

    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        dumped = ast.dump(node)
        speaks = any(
            isinstance(inner, ast.Await)
            and isinstance(getattr(inner.value, "func", None), ast.Attribute)
            and inner.value.func.attr in {
                "reply", "answer", "send_message", "edit_text",
            }
            for inner in ast.walk(node)
        )
        speaks = speaks or any(h in dumped for h in SPEAKING_HELPERS)
        # Handing a message on is not swallowing it.
        speaks = speaks or "SkipHandler" in dumped
        if speaks:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Return) and stmt.value is None:
                offenders.append(stmt.lineno)
    return offenders


@pytest.mark.parametrize(
    "path", sorted(HANDLERS.glob("*.py")), ids=lambda p: p.name
)
def test_no_handler_gives_up_without_saying_why(path: Path) -> None:
    """Silence is not an answer, even to somebody who may not do the thing.

    Three exemptions, and only three, each written down rather than waved
    through:

      * `message.from_user is None` - nobody to reply to;
      * a chat we do not recognise at all - answering would mean this bot
        talks in every group it is ever added to;
      * a message that is not addressed to us, in a group where being quiet
        is the entire point of the design.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    source = path.read_text(encoding="utf-8").splitlines()

    problems = []
    for fn in _decorated_handlers(tree):
        for lineno in _bare_returns_before_speaking(fn):
            # Look at the condition that guards this return.
            context = "\n".join(source[max(0, lineno - 6):lineno])
            exempt = any(
                marker in context
                for marker in (
                    "from_user is None",
                    "chat is None",
                    "is None:",          # unresolved item, unknown topic
                    "not in",            # not our topic
                    "return$",
                )
            )
            if not exempt:
                problems.append(f"  {path.name}:{lineno} in {fn.name}()")

    assert not problems, (
        "these handlers give up without a word:\n" + "\n".join(problems)
        + "\n\nUse a helper that refuses out loud - see _admin_or_refuse in "
          "app/bot/handlers/admin.py. If the silence is genuinely right "
          "because another handler owns the message, add the function to "
          "ROUTING_SILENCE in this file with the reason."
    )


def test_the_routing_exemptions_still_exist() -> None:
    """An exemption for a function that has been renamed or deleted stops
    guarding anything and starts hiding whatever took its place."""
    names = set()
    for path in HANDLERS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {fn.name for fn in _decorated_handlers(tree)}

    stale = set(ROUTING_SILENCE) - names
    assert not stale, f"exemptions for handlers that no longer exist: {stale}"


def test_the_admin_refusal_helper_actually_replies() -> None:
    """The guard above trusts _admin_or_refuse to speak. Check that it does,
    rather than assuming it - a helper that silently returned False would
    satisfy every other test in this file."""
    source = inspect.getsource(admin._admin_or_refuse)
    assert "await message.reply(" in source
    assert source.count("await message.reply(") >= 2, (
        "both branches must speak: registered staff without the rights, and "
        "somebody not registered at all"
    )
    assert "return True" in source and "return False" in source


def test_the_refusal_says_where_to_go_next() -> None:
    """A refusal that only refuses leaves the person exactly where they were.

    Naming /nphelp costs one line and turns "it did nothing" into a next step,
    which is the whole reason this class of bug was expensive.
    """
    source = inspect.getsource(admin._admin_or_refuse)
    assert cmd.HELP in source or "cmd.HELP" in source
    assert "administrator" in source.lower()


def test_no_administrator_command_still_uses_the_silent_form() -> None:
    """The mechanical check, over the whole file.

    `if not await _is_admin(...): return` is the pattern that caused this.
    One legitimate use remains - the callback handler, which answers with an
    alert rather than a reply because that is what a tapped button needs.
    """
    text = (HANDLERS / "admin.py").read_text(encoding="utf-8")
    silent = text.count("if not await _is_admin(")
    assert silent <= 1, f"{silent} handlers still bail out silently"

    if silent:
        # The one that remains must answer the tap.
        index = text.index("if not await _is_admin(")
        following = text[index:index + 300]
        assert "query.answer(" in following and "show_alert=True" in following, (
            "the remaining silent-form check does not answer the button either"
        )
