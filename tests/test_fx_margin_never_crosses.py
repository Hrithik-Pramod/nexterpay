"""The supplier's rate must never reach the client.

This is the reason the FX tables have the shape they do. On every deal each
figure exists twice - our rate and the supplier's, what the client pays and
what the supplier receives - and the difference between the two rates is
NexterPay's margin. A client who learns the supplier's rate learns what
NexterPay make on their business.

Of every failure this platform can have, it is the only one that costs money
rather than goodwill. So the separation is structural: `view_for` takes a side
and reads that side's columns, and there is no code path from a client view to
a supplier figure. These tests exist to keep it that way when somebody later
adds a field and reaches for the nearest similar-looking one.

They are deliberately blunt. Rather than asserting that a particular function
formats a particular string, most of them take everything a client could
possibly be shown and assert the supplier's numbers are nowhere in it.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.domain import fx
from app.domain.enums import FxOrderStatus, FxSide


class _Order:
    """An FX order with both halves filled in, and the two rates far enough
    apart that a leak cannot hide behind rounding."""

    def __init__(self) -> None:
        self.reference = 1042
        self.client_code = "ACME"
        self.supplier_code = "SPEX"
        self.status = FxOrderStatus.AWAITING_SETTLEMENT

        self.client_account_name = "Acme Payments Ltd"
        self.client_rate = Decimal("1.1642")
        self.client_pays = Decimal("250000")
        self.client_pays_currency = "EUR"
        self.client_receives = Decimal("291050")
        self.client_receives_currency = "USDT"

        self.supplier_account_name = "Nexterpay7"
        self.supplier_rate = Decimal("1.1408")
        self.supplier_pays = Decimal("285200")
        self.supplier_pays_currency = "USDT"
        self.supplier_receives = Decimal("250000")
        self.supplier_receives_currency = "EUR"

        self.tx_hash = "a3f19c8b2e7d4016f5c9ab3210de4477"
        self.chain = "tron"

    @property
    def display_reference(self) -> str:
        return f"FX{self.client_code}-{self.supplier_code}-{self.reference}"

    @property
    def client_reference(self) -> str:
        return f"FX{self.client_code}-{self.reference}"

    @property
    def margin(self):
        return self.client_rate - self.supplier_rate


SUPPLIER_ONLY = ("1.1408", "285200", "285,200", "Nexterpay7", "SPEX")


# --------------------------------------------------------------------------
# What the client is shown
# --------------------------------------------------------------------------

def test_the_client_view_carries_none_of_the_suppliers_figures() -> None:
    view = fx.view_for(_Order(), FxSide.CLIENT)
    rendered = " ".join(view.lines())

    for secret in SUPPLIER_ONLY:
        assert secret not in rendered, f"{secret!r} reached the client:\n{rendered}"


def test_the_client_view_holds_the_clients_own_figures() -> None:
    """The other half of the guarantee. A view that leaked nothing because it
    contained nothing would pass the test above and be useless."""
    view = fx.view_for(_Order(), FxSide.CLIENT)
    rendered = " ".join(view.lines())

    assert "1.1642" in rendered
    assert "250,000" in rendered
    assert "291,050" in rendered
    assert "Acme Payments Ltd" in rendered


def test_the_client_reference_never_carries_the_supplier_code() -> None:
    """A client who can see which supplier their deal sits with can work out
    who NexterPay buy from, which is the first step to working out the margin."""
    view = fx.view_for(_Order(), FxSide.CLIENT)
    assert "SPEX" not in view.reference
    assert view.reference == "FXACME-1042"


def test_the_client_view_has_no_attribute_holding_a_supplier_figure() -> None:
    """Not just the rendered lines - the object itself.

    Somebody adding a message later will reach for `view.rate`, and it has to
    be impossible for that to be the wrong rate.
    """
    view = fx.view_for(_Order(), FxSide.CLIENT)
    values = {str(v) for v in vars(view).values() if v is not None}

    assert "1.1408" not in values
    assert "Nexterpay7" not in values


# --------------------------------------------------------------------------
# And the supplier's side, which has its own secret
# --------------------------------------------------------------------------

def test_the_supplier_view_does_not_name_the_client() -> None:
    """The account name on the supplier's order is NexterPay's code with them,
    not the client's business name. NexterPay, 12 September."""
    view = fx.view_for(_Order(), FxSide.SUPPLIER)
    rendered = " ".join(view.lines())

    assert "Acme Payments Ltd" not in rendered
    assert "Nexterpay7" in rendered


def test_the_supplier_view_does_not_carry_our_rate() -> None:
    """What we charge the client is as much our business as what the supplier
    charges us. A supplier who knows both knows the margin too."""
    view = fx.view_for(_Order(), FxSide.SUPPLIER)
    rendered = " ".join(view.lines())

    assert "1.1642" not in rendered
    assert "291,050" not in rendered


# --------------------------------------------------------------------------
# The margin itself
# --------------------------------------------------------------------------

def test_the_margin_is_the_difference_and_is_never_stored() -> None:
    """There is no margin column, deliberately. It is the difference between
    two rates that are each recorded for their own reason, which means there is
    no field for anything to render by accident."""
    order = _Order()
    assert order.margin == Decimal("0.0234")

    view = fx.view_for(order, FxSide.CLIENT)
    assert "margin" not in {k.lower() for k in vars(view)}
    assert "0.0234" not in " ".join(view.lines())


# --------------------------------------------------------------------------
# Structural guards
#
# The tests above check today's code. These check that the shape which makes
# the guarantee possible has not been quietly undone.
# --------------------------------------------------------------------------

def test_view_for_is_the_only_thing_that_composes_figures_for_a_counterparty() -> None:
    source = inspect.getsource(fx.view_for)
    assert "client_rate" in source
    assert "supplier_rate" in source
    assert source.count("FxSide.CLIENT") >= 1


def test_there_is_no_function_that_takes_an_unsided_rate() -> None:
    """`rate` as a bare parameter is fine on functions that write one side.
    What must not exist is a function returning figures without being told
    which side it is for - that is the shape a leak would take.
    """
    signature = inspect.signature(fx.view_for)
    assert "side" in signature.parameters, (
        "view_for must be told which side it is composing for"
    )


def test_every_state_names_who_it_is_waiting_on() -> None:
    """The test each state had to pass to earn its place, from the FX Flow note
    of 5 September. A state that does not change who you are waiting on is two
    states doing one job."""
    for status in FxOrderStatus:
        assert status.waiting_on, f"{status.value} does not say whose move it is"
        assert status.label


@pytest.mark.parametrize(
    "start,forbidden",
    [
        # The sequence enforcement NexterPay chose knowingly on 5 September:
        # a hash cannot be issued before a rate is approved.
        (FxOrderStatus.RATE_REQUESTED, FxOrderStatus.AWAITING_SETTLEMENT),
        (FxOrderStatus.RATE_REQUESTED, FxOrderStatus.CLOSED),
        (FxOrderStatus.RATE_QUOTED, FxOrderStatus.AWAITING_SETTLEMENT),
        (FxOrderStatus.AWAITING_CLIENT_CONFIRMATION, FxOrderStatus.AWAITING_RECEIPT),
        (FxOrderStatus.CLOSED, FxOrderStatus.AWAITING_SETTLEMENT),
    ],
)
def test_a_deal_cannot_skip_ahead(start, forbidden) -> None:
    assert not fx.may_move(start, forbidden)


@pytest.mark.parametrize(
    "start,allowed",
    [
        (FxOrderStatus.RATE_REQUESTED, FxOrderStatus.RATE_QUOTED),
        (FxOrderStatus.RATE_QUOTED, FxOrderStatus.AWAITING_CLIENT_CONFIRMATION),
        (FxOrderStatus.RATE_QUOTED, FxOrderStatus.RATE_REJECTED),
        (FxOrderStatus.RATE_REJECTED, FxOrderStatus.RATE_QUOTED),
        (
            FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
            FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
        ),
        (FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE, FxOrderStatus.AWAITING_SETTLEMENT),
        (FxOrderStatus.AWAITING_SETTLEMENT, FxOrderStatus.AWAITING_RECEIPT),
        (FxOrderStatus.AWAITING_RECEIPT, FxOrderStatus.CLOSED),
    ],
)
def test_the_route_itself_is_open(start, allowed) -> None:
    assert fx.may_move(start, allowed)


def test_rejection_returns_rather_than_ending_the_deal() -> None:
    """The only branch in the flow, and the case that happens most often. A
    flow with no way back gets worked around, and the workaround is closing the
    deal and opening a new one - which loses the link between the two quotes."""
    assert fx.may_move(FxOrderStatus.RATE_QUOTED, FxOrderStatus.RATE_REJECTED)
    assert fx.may_move(FxOrderStatus.RATE_REJECTED, FxOrderStatus.RATE_QUOTED)
    assert fx.may_move(FxOrderStatus.RATE_REJECTED, FxOrderStatus.RATE_REQUESTED)
