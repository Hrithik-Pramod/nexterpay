"""Watching the wallet suppliers pay into.

Jason, 3 October: one Tron address, everything lands there, and "need the
ability to change monitoring of wallet". Read-only was agreed in the same
conversation - it watches, it never holds a key.

One wallet is the fact that shapes all of this. A payment says how much and
when; it does not say who sent it or what it is for. So matching is on amount,
and the only interesting behaviour is where it refuses.

The tolerance is not a convenience either. On 7 September the desk paid
163,378 USDT against orders totalling 163,379.07, having rounded both lines
down by hand - Jason confirmed a slip. A matcher demanding exactness would
have failed to recognise the very payment it was waiting for, which is worse
than recognising it and reporting that it is 1.07 short.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services import wallet

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _payment(amount, *, at=NOW, tx="a" * 64):
    return wallet.IncomingPayment(
        tx_hash=tx, amount_usdt=Decimal(amount), at=at
    )


# --------------------------------------------------------------------------
# Reading what the chain reports
# --------------------------------------------------------------------------

def test_usdt_has_six_decimals_on_tron():
    assert wallet.from_micro_usdt(1_000_000) == Decimal("1")
    assert wallet.from_micro_usdt("39309469000") == Decimal("39309.469")


def test_the_conversion_never_touches_a_float():
    """0.1 + 0.2 is not 0.3 in binary floating point, and this is money."""
    assert isinstance(wallet.from_micro_usdt(1), Decimal)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def test_a_payment_matching_one_outstanding_settlement():
    outstanding = [
        wallet.Candidate("SET-1000", Decimal("39309.469")),
        wallet.Candidate("SET-1001", Decimal("12000")),
    ]

    proposal = wallet.match_payment(_payment("39309.469"), outstanding)

    assert proposal.is_certain
    assert proposal.candidates[0].key == "SET-1000"
    assert proposal.difference() == Decimal("0")


def test_their_rounding_slip_still_matches():
    """163,378 paid against 163,379.07 expected. The payment is the one being
    waited for; it is just a pound light.

    This failed in the first version of this module, which used a one-USDT
    window because the settlement checker does - and 1.07 is outside it. The
    matcher rejected the exact payment it existed to recognise.
    """
    outstanding = [wallet.Candidate("SET-1000", Decimal("163379.07"))]

    proposal = wallet.match_payment(_payment("163378"), outstanding)

    assert proposal.is_certain
    assert round(proposal.difference(), 2) == Decimal("-1.07")


def test_two_outstanding_amounts_within_tolerance_are_not_guessed_between():
    """The coin flip this refuses to make.

    Taking the nearest would resolve two suppliers owing amounts half a USDT
    apart to whichever happened to be marginally closer - silently, and with
    a client told their money arrived when it did not.
    """
    outstanding = [
        wallet.Candidate("SET-1000", Decimal("50000.40")),
        wallet.Candidate("SET-1001", Decimal("50000.60")),
    ]

    proposal = wallet.match_payment(_payment("50000.50"), outstanding)

    assert proposal.is_ambiguous
    assert {c.key for c in proposal.candidates} == {"SET-1000", "SET-1001"}
    assert proposal.difference() is None


def test_a_payment_nobody_was_expecting_is_reported_not_discarded():
    """Either a supplier paying early, or money arriving that nobody has
    accounted for. Both are worth saying out loud."""
    outstanding = [wallet.Candidate("SET-1000", Decimal("39309.469"))]

    proposal = wallet.match_payment(_payment("777"), outstanding)

    assert proposal.is_unexpected
    assert proposal.payment.amount_usdt == Decimal("777")


def test_a_payment_well_outside_tolerance_does_not_match():
    outstanding = [wallet.Candidate("SET-1000", Decimal("39309.469"))]
    assert wallet.match_payment(_payment("777"), outstanding).is_unexpected


def test_the_window_scales_with_the_amount():
    """Their slip came from truncating each line to whole USDT before adding,
    so a seven-line settlement can be seven out by the same mechanism. A flat
    figure would be generous on a hundred-dollar payment and mean on a
    hundred-thousand one."""
    assert wallet.tolerance_for(Decimal("100")) == wallet.MATCH_FLOOR
    assert round(wallet.tolerance_for(Decimal("163379.07")), 2) == Decimal("16.34")


def test_the_two_tolerances_are_different_questions():
    """`settlement.TOLERANCE_USDT` asks whether a difference is worth
    mentioning; one USDT is right for that, and their 1.07 slip should be
    reported. This one asks whether a payment is the one being waited for,
    and the first version of this module conflated them - a one-USDT window
    rejected the very payment it was built to recognise.

    Erring wide is the cheaper mistake. Too narrow and a real payment looks
    like money nobody expected, which costs somebody a day. Too wide and two
    amounts both fall in the window, which this reports as ambiguous and
    refuses to resolve - so it costs a question.
    """
    from app.domain import settlement

    assert wallet.MATCH_FLOOR > settlement.TOLERANCE_USDT


def test_the_difference_keeps_its_sign():
    outstanding = [wallet.Candidate("SET-1000", Decimal("100"))]
    assert wallet.match_payment(_payment("99.5"), outstanding).difference() < 0
    assert wallet.match_payment(_payment("100.5"), outstanding).difference() > 0


# --------------------------------------------------------------------------
# Several payments at once
# --------------------------------------------------------------------------

def test_one_settlement_cannot_answer_two_payments():
    """Two suppliers sending the same amount is ordinary. Both payments
    claiming one settlement would mark it paid twice and leave the other
    outstanding item looking unpaid."""
    outstanding = [
        wallet.Candidate("SET-1000", Decimal("5000")),
        wallet.Candidate("SET-1001", Decimal("5000")),
    ]
    payments = [
        _payment("5000", at=NOW, tx="a" * 64),
        _payment("5000", at=NOW + timedelta(minutes=5), tx="b" * 64),
    ]

    proposals = wallet.match_all(payments, outstanding)

    # Both are ambiguous here, which is correct - two identical outstanding
    # amounts cannot be told apart by amount alone, and that is the whole
    # point of refusing.
    assert all(p.is_ambiguous for p in proposals)


def test_a_certain_match_is_taken_out_of_play():
    outstanding = [
        wallet.Candidate("SET-1000", Decimal("5000")),
        wallet.Candidate("SET-1001", Decimal("9000")),
    ]
    payments = [
        _payment("5000", at=NOW, tx="a" * 64),
        _payment("5000", at=NOW + timedelta(minutes=5), tx="b" * 64),
    ]

    first, second = wallet.match_all(payments, outstanding)

    assert first.is_certain and first.candidates[0].key == "SET-1000"
    assert second.is_unexpected, "the settlement it would have claimed is gone"


def test_payments_are_considered_in_the_order_they_arrived():
    """The only ordering that is not arbitrary."""
    outstanding = [wallet.Candidate("SET-1000", Decimal("5000"))]
    later = _payment("5000", at=NOW + timedelta(hours=1), tx="b" * 64)
    earlier = _payment("5000", at=NOW, tx="a" * 64)

    proposals = wallet.match_all([later, earlier], outstanding)

    assert proposals[0].payment.tx_hash == "a" * 64
    assert proposals[0].is_certain


def test_nothing_outstanding_means_everything_is_unexpected():
    assert wallet.match_payment(_payment("5000"), []).is_unexpected


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------

def test_this_module_cannot_move_money() -> None:
    """Read-only, agreed with Jason on 3 October and asserted here because the
    next person to work on this file will not have read that conversation.

    A key, a signature or a broadcast appearing in this module means the
    boundary has moved, and moving it should be a conversation rather than a
    commit.
    """
    source = pathlib.Path("app/services/wallet.py").read_text(encoding="utf-8")

    forbidden = (
        "private_key", "privatekey", "sign(", "sign_transaction",
        "broadcast", "send_transaction", "transfer(", "mnemonic", "seed_phrase",
    )
    offenders = [
        word for word in forbidden
        # The docstring says what this module will not do; that sentence is
        # not an implementation of it.
        if word in source.replace("signs nothing", "")
        .replace("needs a key", "").replace("holds no key", "")
    ]
    assert not offenders, (
        f"wallet.py mentions {offenders}. This module watches an address and "
        f"nothing else - it holds no key and moves no funds. If that has "
        f"changed, it was a conversation with NexterPay, not a refactor."
    )


def test_the_chain_client_is_an_interface_not_an_import() -> None:
    """Nothing in this suite should need a network.

    A module that reached for Tron directly could only be exercised by
    pretending, which is the kind of test that passes while the thing it
    covers is broken.
    """
    source = pathlib.Path("app/services/wallet.py").read_text(encoding="utf-8")
    for network in ("import httpx", "import requests", "import aiohttp", "urlopen"):
        assert network not in source
    assert "class ChainClient(Protocol)" in source
