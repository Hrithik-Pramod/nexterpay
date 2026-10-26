"""An order must not quietly change a rate the counterparty agreed.

Found on 26 October driving a deal end to end. FXACME-SPEX-1006 was quoted to
the client at 600, the client was asked

    Rate on XOF is 600. Would you like to proceed?

and tapped Yes. `/npordercl` then asked for the rate again - it does, the
order step collects rate, amount and what they receive together - 590 was
typed, and the order that came back for approval read:

    This will be sent to Acme Payments as the client side:
    Account: Acme Payments
    Rate: 590
    They send: 35,000 USDT
    They receive: 20,650,000 XOF

At the rate they agreed the client would have received 21,000,000 XOF. They
were 350,000 short, on an order they had already said yes to, and nothing on
that screen or in the Operations topic said the rate had moved.

Warned rather than refused. There are honest reasons for a different number,
and `/npamend` exists for the case where the rate has genuinely moved - it
puts the new rate back to the counterparty. What there is no reason for is
sending it without noticing, which is what a typo in that field did.

The supplier side gets the same treatment from the same code, and that is
worth a test of its own: a supplier order at a rate the supplier never quoted
is NexterPay's own margin moving, which nobody outside would ever report.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot.handlers.fx import Figures, preview_text
from app.domain.enums import FxSide

FIGURES = Figures(
    rate=Decimal("590"),
    pays=Decimal("35000"),
    pays_currency="USDT",
    receives=Decimal("20650000"),
    receives_currency="XOF",
)


def test_a_rate_that_differs_from_the_agreed_one_is_called_out() -> None:
    shown = preview_text(
        FxSide.CLIENT, "Acme Payments", FIGURES, "Acme Payments",
        agreed_rate=Decimal("600"),
    )

    assert "600" in shown and "590" in shown, (
        "the desk cannot compare two rates they are not both shown"
    )
    assert "⚠️" in shown
    assert "npamend" in shown, (
        "a desk that means to change the rate needs pointing at the path that "
        "asks the counterparty again"
    )
    # And it still says everything it said before.
    assert "Nothing has been sent yet." in shown
    assert "35,000 USDT" in shown


def test_the_warning_names_the_counterparty_being_changed_on() -> None:
    """Two deals open at once, and "the client" is a role rather than a name."""
    shown = preview_text(
        FxSide.SUPPLIER, "Supplier Pexi", FIGURES, "Pexi Ltd",
        agreed_rate=Decimal("585"),
    )

    assert "Supplier Pexi agreed 585" in shown, (
        "a supplier order at a rate the supplier never quoted moves "
        "NexterPay's own margin, and nobody outside would report it"
    )


@pytest.mark.parametrize("agreed", [Decimal("590"), Decimal("590.00"), None])
def test_nothing_is_said_when_the_rate_is_the_one_agreed(agreed) -> None:
    """Including a deal with no rate on it yet, and trailing zeroes.

    `Decimal("590") != Decimal("590.00")` is False - they compare equal - and
    this is here so that stays true if the comparison is ever rewritten as
    strings, which would warn on every well-formed order and teach the desk
    to tap past the warning.
    """
    shown = preview_text(
        FxSide.CLIENT, "Acme Payments", FIGURES, "Acme Payments",
        agreed_rate=agreed,
    )

    assert "⚠️" not in shown
    assert "agreed" not in shown
    assert shown.startswith("This will be sent to Acme Payments")


def test_the_preview_reads_the_rate_through_the_one_side_aware_function() -> None:
    """Not `order.client_rate` directly.

    `fx.view_for` is the only thing on this platform that reads a side's
    columns, and the module is built so that a leak needs somebody to call the
    wrong function rather than merely to forget. Reading `client_rate` here
    would compare a supplier order against the client's rate and warn about
    the wrong number - or worse, not warn.
    """
    import ast
    import inspect
    import textwrap

    from app.bot.handlers import fx as handlers

    source = textwrap.dedent(inspect.getsource(handlers.capture_name))
    tree = ast.parse(source)
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    assert "fx.view_for" in calls, (
        "the order preview no longer reads the agreed rate through view_for"
    )

    # Attribute reads off the AST, not words in the file. The comment above
    # this code explains the trap by naming `order.client_rate`, and a guard
    # that greps would fail on the explanation of why it exists - which is
    # exactly how a guard gets deleted rather than understood.
    read = {
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and ast.unparse(n).startswith("order.")
    }
    assert not {"order.client_rate", "order.supplier_rate"} & read, (
        f"the order preview reads a side's column directly: {sorted(read)}"
    )
