"""Every call a handler makes into the service layer must actually fit.

Written after breaking the platform in production on 5 September. The
`keyboard=` parameter was removed from `relay.open_request` and the call site
in `client.py` was left passing it, so every attempt to raise a request - in
every client group, by every route - died with a TypeError before reaching the
acknowledgement. NexterPay found it within the hour by typing into a group.

All 342 tests passed. They call `relay.open_request(...)` directly and not one
of them goes through the handler, so the handler's call was never executed.
That is the same gap that hid the broadcast bug in August: tested at the
service layer, broken at the handler layer, invisible in between.

The honest fix is end-to-end tests through aiogram, which is a large piece of
work. This is the cheap 90%: bind every call in `app/bot/` to the signature of
the function it names, statically, and fail if it would raise. It costs
milliseconds and catches the entire class.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.services import relay

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "app" / "bot"

# module alias in the handlers -> the real module
WATCHED = {"relay": relay}


def _calls_in(path: Path):
    """Every `relay.something(...)` call, with the keywords it passes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if not isinstance(owner, ast.Name) or owner.id not in WATCHED:
            continue
        yield node, owner.id, func.attr


def _sources():
    return sorted(BOT.rglob("*.py"))


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_every_service_call_matches_its_signature(path: Path) -> None:
    """The check that was missing.

    `inspect.Signature.bind` is exactly the rule Python applies at runtime, so
    a call that binds here is a call that will not raise TypeError there.
    Positional arguments are passed as placeholders because only their number
    matters; the keywords are what drift.
    """
    problems = []

    for node, alias, name in _calls_in(path):
        target = getattr(WATCHED[alias], name, None)
        if target is None:
            problems.append(
                f"  {path.name}:{node.lineno}  {alias}.{name} does not exist"
            )
            continue
        if not callable(target):
            continue

        signature = inspect.signature(target)
        positional = [object()] * len(node.args)
        keywords = {}
        for kw in node.keywords:
            if kw.arg is None:      # **kwargs at the call site - unknowable
                keywords = None
                break
            keywords[kw.arg] = object()
        if keywords is None:
            continue

        try:
            signature.bind(*positional, **keywords)
        except TypeError as exc:
            problems.append(
                f"  {path.name}:{node.lineno}  {alias}.{name}(...) — {exc}"
            )

    assert not problems, (
        "these calls would raise TypeError the moment somebody used them:\n"
        + "\n".join(problems)
    )


def test_the_guard_is_actually_looking_at_something() -> None:
    """A checker that finds no calls passes for the wrong reason.

    If the handlers stop importing relay under that name - or this file's
    idea of where they live goes stale - the parametrised test above would
    quietly succeed over an empty set forever.
    """
    found = sum(1 for path in _sources() for _ in _calls_in(path))
    assert found > 20, f"only {found} service calls found; the scan has gone blind"


def test_open_request_no_longer_takes_a_keyboard() -> None:
    """The specific regression, pinned.

    Buttons on a header are removed by the first `refresh_header`, so the
    parameter was taken away rather than documented. It must stay away: the
    danger is not the parameter, it is that someone finds it and uses it.
    """
    assert "keyboard" not in inspect.signature(relay.open_request).parameters
    assert "keyboard" not in inspect.signature(relay.open_outbound).parameters
    # open_internal keeps a callable, which is a different thing: it is handed
    # the new id and its result goes on a separate message.
    assert "keyboard_for" in inspect.signature(relay.open_internal).parameters
