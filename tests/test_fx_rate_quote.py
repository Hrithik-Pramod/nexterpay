"""Sending the client the rate, and asking whether to proceed.

NexterPay, 16 September:

    Point 12, if we have the rates, we should have option to send the client a
    message — "rate on (currency code) is XXX". Then client asked, would you
    like to proceed, Yes Or No. Then we create clients order.

This reverses a decision. `send_rate_quote` was deliberately not built: quoting
a client is a conversation, and the desk said it in their own words through
Reply to Client. NexterPay asked for it outright, which is theirs to ask — and
the reason the decision was written down as a decision rather than quietly
made is so that reversing it costs one message rather than an argument.

It makes a fourth function in `fx_relay` that writes to a counterparty. That
list is the safety property of the whole FX build, so the new one earns its
place the same way the others do: it composes through `fx.view_for`, which
reads one side's columns and cannot see the other's. The test that matters
most in this file is the one asserting the supplier's rate is not in the
message.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot import commands as cmd
from app.bot.handlers import fx as handlers
from app.domain import fx
from app.domain.enums import EventType, FxSide
from app.services import fx_relay


class _Order:
    """Enough of a deal to compose a sentence about a rate."""

    def __init__(self, currency="INR", client_rate="90.00", supplier_rate="89.50"):
        self.reference = 1000
        self.client_code = "ACME"
        self.supplier_code = "SPEX"
        self.currency_code = currency
        self.client_rate = Decimal(client_rate)
        self.supplier_rate = Decimal(supplier_rate)
        self.client_account_name = "Acme Payments Ltd"
        self.supplier_account_name = "Nexterpay7"
        self.client_pays = self.client_receives = None
        self.client_pays_currency = self.client_receives_currency = None
        self.supplier_pays = self.supplier_receives = None
        self.supplier_pays_currency = self.supplier_receives_currency = None

    @property
    def client_reference(self):
        return f"FX{self.client_code}-{self.reference}"

    @property
    def supplier_reference(self):
        return f"FX{self.supplier_code}-{self.reference}"


# --------------------------------------------------------------------------
# The currency code, which is what makes a rate a price
# --------------------------------------------------------------------------

def test_a_three_letter_code_is_accepted_and_upper_cased() -> None:
    assert fx.parse_currency_code("inr") == "INR"
    assert fx.parse_currency_code("  NGN  ") == "NGN"


def test_anything_else_is_refused() -> None:
    """"Indian Rupees" in a client's message is a different sentence somebody
    then has to check."""
    for bad in ("", "   ", "INRR", "IN", "IN1", "rupees", "₹"):
        with pytest.raises(fx.FxError):
            fx.parse_currency_code(bad)


def test_the_currency_reaches_both_views() -> None:
    """Both halves of a deal are priced in the same local currency — it is a
    property of the deal, not of a side — but it has to be on the view, or
    something outside has to reach past `view_for` to write a sentence."""
    order = _Order()
    assert fx.view_for(order, FxSide.CLIENT).currency_code == "INR"
    assert fx.view_for(order, FxSide.SUPPLIER).currency_code == "INR"


# --------------------------------------------------------------------------
# The message itself
# --------------------------------------------------------------------------

def test_the_message_is_in_their_shape() -> None:
    """NexterPay wrote the sentence: "rate on (currency code) is XXX"."""
    text = fx_relay.rate_quote_text(_Order())
    assert "on INR" in text
    assert "90.00" in text or "90" in text
    assert "proceed" in text.lower()


def test_the_client_reference_is_used() -> None:
    text = fx_relay.rate_quote_text(_Order())
    assert "FXACME-1000" in text
    assert "SPEX" not in text


def test_the_suppliers_rate_is_not_in_it() -> None:
    """The one that matters.

    A fourth way out of NexterPay is exactly how a leak arrives — not by
    somebody rewriting `view_for`, but by a new message that reads the order
    directly and picks up the wrong column.
    """
    text = fx_relay.rate_quote_text(_Order(client_rate="90.00", supplier_rate="89.50"))
    assert "89.5" not in text
    assert "Nexterpay7" not in text


def test_a_deal_with_no_currency_still_says_something() -> None:
    """Older deals predate the currency column. A rate with no currency is a
    poorer message than one with, and a far better one than a crash."""
    text = fx_relay.rate_quote_text(_Order(currency=None))
    assert "90" in text
    assert "None" not in text


# --------------------------------------------------------------------------
# Yes and No
# --------------------------------------------------------------------------

def test_the_client_gets_both_answers() -> None:
    """An order has one button because the figures were already agreed in
    words. A rate is an offer, and turning one down is an ordinary answer that
    should not require typing."""
    buttons = [
        b for row in handlers.rate_decision_keyboard(7).inline_keyboard for b in row
    ]
    assert len(buttons) == 2
    data = {b.callback_data for b in buttons}
    assert data == {"fx:rateyes:7", "fx:rateno:7"}


def test_yes_is_not_the_same_event_as_confirming_an_order() -> None:
    """Saying yes to a price and agreeing a set of figures are different
    promises. Six weeks later a dispute turns on which one was given, so the
    history has to be able to tell them apart."""
    assert EventType.FX_RATE_ACCEPTED is not EventType.FX_CLIENT_CONFIRMED


def test_accepting_a_rate_does_not_move_the_deal() -> None:
    """There are no amounts yet.

    Moving it to Awaiting client confirmation here would leave a deal reading
    as though the client had something to confirm, when the desk has not built
    it.
    """
    import ast
    import inspect

    source = ast.unparse(ast.parse(inspect.getsource(fx.client_accepts_rate)))
    assert "_move(" not in source


def test_no_reuses_the_rejection_that_already_exists() -> None:
    """Not a new state — the return path NexterPay described on 12 September,
    reached by a button instead of the desk typing what the client said."""
    import ast
    import inspect

    source = ast.unparse(
        ast.parse(inspect.getsource(handlers.client_declines_the_rate))
    )
    assert "reject_client_rate" in source


# --------------------------------------------------------------------------
# Asking every supplier at once
# --------------------------------------------------------------------------

def test_the_rate_check_command_is_registered() -> None:
    from aiogram.filters import Command

    registered = set()
    for handler in handlers.router.message.handlers:
        for f in handler.filters or []:
            if isinstance(f.callback, Command):
                registered.update(str(c) for c in f.callback.commands)

    assert cmd.RATE_CHECK in registered
    assert cmd.RATE_CHECK in cmd.ALL


def test_every_supplier_is_asked_the_same_question() -> None:
    """One wording for all of them. The replies are read side by side, and five
    differently-phrased questions produce five differently-shaped answers."""
    assert "1 USDT" in handlers.RATE_CHECK_BODY
    assert "how long it holds" in handlers.RATE_CHECK_BODY


def test_nothing_is_sent_before_the_list_is_seen() -> None:
    """This is the one command that writes into every supplier group at once,
    so it previews first like everything else that reaches outside."""
    import ast
    import inspect

    source = ast.unparse(ast.parse(inspect.getsource(handlers.rate_check)))
    assert "Nothing has been sent yet." in source
    assert "open_outbound" not in source, (
        "the command sends without showing the list first"
    )
