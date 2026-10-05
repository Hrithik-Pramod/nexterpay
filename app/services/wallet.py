"""Watching the wallet NexterPay's suppliers pay into.

Jason, 3 October: a Tron address, and "need the ability to change monitoring
of wallet". Asked whether that was one wallet or one per Nexterpay account, he
answered A - one wallet, everything lands there.

**This is read-only and will stay read-only.** It watches an address. It holds
no key, signs nothing and moves nothing, and the only thing it can do with a
payment it finds is tell somebody about it. That boundary was agreed with
Jason in the same conversation and it is written here as well as there,
because the next person to work on this file will not have read the WhatsApp
thread. If a requirement ever arrives that needs a key, it does not belong in
this module.

**One wallet has a consequence.** A payment arriving says how much and when;
it does not say which supplier sent it or what it is for. So matching is on
amount, against the settlements the desk is waiting for - which works, and
has to refuse to guess when two outstanding requests are close together.
Attaching a payment to the wrong supplier's settlement would tell one client
their money has arrived when it has not.

**And the tolerance is not optional.** On 7 September the desk sent 163,378
USDT against orders totalling 163,379.07, having rounded down by hand. Jason
confirmed that was a slip. A matcher demanding exactness would have failed to
recognise a payment that genuinely was the one being waited for, which is a
worse outcome than matching it and saying it is 1.07 short.

The chain client is injected rather than imported. Nothing in this project's
test suite should need a network, and a module that reached for Tron directly
would be a module the suite could only exercise by pretending.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

# The one module that decides which column holds the local amount. Imported
# rather than copied: this file and `settlement.py` each made the same wrong
# choice independently, which is what two copies of a decision buys you.
from app.domain import settlement

logger = logging.getLogger(__name__)

# USDT on Tron. Six decimals, so the raw integer in a transfer is micro-USDT.
USDT_DECIMALS = Decimal("1000000")

# How far a payment may sit from an expected total and still be recognised as
# that payment.
#
# This is **not** the same question as `settlement.TOLERANCE_USDT`, and the
# first version of this module got that wrong by assuming it was. That one
# asks "is this difference worth mentioning", and one USDT is right for it -
# their 1.07 slip should be reported. This one asks "is this the same
# payment", and a one-USDT window rejected the very payment it was built for,
# because 1.07 is outside it. A payment that is recognisably the one being
# waited for, and a pound light, is not an unexpected payment.
#
# The two errors are not symmetrical. Too narrow and a real payment looks
# like money nobody was expecting, which is the failure that costs a day of
# somebody's time. Too wide and two outstanding amounts both fall in the
# window - which this module reports as ambiguous and refuses to resolve, so
# it costs a question. Erring wide is therefore the cheaper mistake.
#
# A floor rather than a flat figure because the slips scale: theirs came from
# truncating each line to whole USDT before adding, so a seven-line
# settlement can be seven USDT out by exactly the same mechanism.
MATCH_FLOOR = Decimal("5")
MATCH_FRACTION = Decimal("0.0001")   # one basis point


def tolerance_for(expected: Decimal) -> Decimal:
    """The window around an expected amount, for a payment of this size."""
    return max(MATCH_FLOOR, abs(expected) * MATCH_FRACTION)


# Where the watched address is kept. A key rather than a column, and a
# setting rather than an environment variable, because Jason asked for it to
# be changeable and the person who needs to change it is on the finance desk.
WALLET_SETTING = "fx.watched_wallet"

# Tron base58 addresses: `T` then 33 more base58 characters. Base58 excludes
# 0, O, I and l precisely because they are the characters people confuse, so
# refusing anything containing them is refusing a typo rather than an address.
_TRON_ADDRESS = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


class WalletError(Exception):
    """Something that is not an address NexterPay can be paid at."""


def parse_address(text: str) -> str:
    """A Tron address, checked rather than merely stored.

    Checked because of what happens if it is wrong: the watcher looks at an
    address nobody is paying into, finds nothing, and says nothing - and
    silence from a monitor is indistinguishable from nothing having arrived.
    A wrong address here would be discovered by a client chasing a settlement
    that the platform believed had not been made.

    Not a checksum validation. That would need base58 decoding and the
    dependency it brings, and the failure this guards against is a pasted
    address losing characters or picking up whitespace, which the shape
    catches.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise WalletError("Which address? A Tron address starts with T.")
    if not _TRON_ADDRESS.match(cleaned):
        raise WalletError(
            f"“{cleaned}” is not a Tron address. They start with T and are 34 "
            f"characters - this one is {len(cleaned)}."
        )
    return cleaned


@dataclass(frozen=True)
class IncomingPayment:
    """A USDT transfer into the watched address."""

    tx_hash: str
    amount_usdt: Decimal
    at: datetime
    from_address: str | None = None


class ChainClient(Protocol):
    """Whatever can answer "what has arrived".

    Deliberately tiny. The whole surface this project needs from a chain is
    one question, and keeping it to one question is what lets the suite run
    without a network and what would make a different provider a small change
    rather than a rewrite.
    """

    async def incoming_usdt(
        self, address: str, *, since: datetime | None = None
    ) -> list[IncomingPayment]:
        ...


@dataclass(frozen=True)
class Candidate:
    """Something the desk is waiting to be paid."""

    key: str              # how the caller identifies it again - a reference
    expected_usdt: Decimal
    label: str = ""


@dataclass(frozen=True)
class Proposal:
    """A payment, and what it looks like it settles.

    `candidates` holds every outstanding item within tolerance. One is a
    match. More than one is a question for the desk, and none is a payment
    nobody was expecting - which is worth saying out loud rather than
    discarding, because an unexpected payment is either a supplier paying
    early or money arriving that nobody has accounted for.
    """

    payment: IncomingPayment
    candidates: list[Candidate]

    @property
    def is_certain(self) -> bool:
        return len(self.candidates) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def is_unexpected(self) -> bool:
        return not self.candidates

    def difference(self) -> Decimal | None:
        """How far the payment is from what was expected, when that is a
        single answer. Signed: short and over are different problems."""
        if not self.is_certain:
            return None
        return self.payment.amount_usdt - self.candidates[0].expected_usdt


def match_payment(
    payment: IncomingPayment,
    outstanding: list[Candidate],
    *,
    tolerance: Decimal | None = None,
) -> Proposal:
    """Which outstanding settlement does this payment look like?

    Every candidate within tolerance is kept rather than the nearest one. The
    nearest is a tempting answer and a dangerous one: two suppliers owing
    amounts half a USDT apart would resolve silently to whichever happened to
    be marginally closer, which is exactly the coin-flip this refuses to make.
    """
    near = [
        candidate for candidate in outstanding
        if abs(payment.amount_usdt - candidate.expected_usdt)
        <= (tolerance if tolerance is not None
            else tolerance_for(candidate.expected_usdt))
    ]
    return Proposal(payment=payment, candidates=near)


def match_all(
    payments: list[IncomingPayment],
    outstanding: list[Candidate],
    *,
    tolerance: Decimal | None = None,
) -> list[Proposal]:
    """Several payments against the same outstanding list.

    A candidate matched with certainty is taken out of play before the next
    payment is considered, so two payments of the same size do not both claim
    one settlement. Payments are considered in the order they arrived, which
    is the only ordering that is not arbitrary.
    """
    remaining = list(outstanding)
    proposals = []

    for payment in sorted(payments, key=lambda p: p.at):
        proposal = match_payment(payment, remaining, tolerance=tolerance)
        proposals.append(proposal)
        if proposal.is_certain:
            claimed = proposal.candidates[0].key
            remaining = [c for c in remaining if c.key != claimed]

    return proposals


def from_micro_usdt(raw: int | str) -> Decimal:
    """A TRC-20 USDT amount as the chain reports it.

    USDT has six decimals on Tron, so a transfer of one USDT is the integer
    1000000. Done in Decimal throughout - this is somebody's money and
    floating point would quietly round it.
    """
    return Decimal(str(raw)) / USDT_DECIMALS


__all__ = [
    "MATCH_FLOOR",
    "WALLET_SETTING",
    "WalletError",
    "parse_address",
    "MATCH_FRACTION",
    "USDT_DECIMALS",
    "Candidate",
    "ChainClient",
    "IncomingPayment",
    "Proposal",
    "from_micro_usdt",
    "match_all",
    "candidates_from_orders",
    "match_payment",
    "tolerance_for",
]


# --------------------------------------------------------------------------
# What the desk is waiting to be paid
# --------------------------------------------------------------------------

def candidates_from_orders(orders: list) -> list[Candidate]:
    """Open deals, as amounts a payment might be for.

    The expected figure is the supplier's side converted at the supplier's
    rate, because that is the leg this wallet is settling - NexterPay's
    arrangement with the supplier, not what the client pays. Reading the
    client's columns here would mean matching payments against the margin as
    well as the amount, and finding nothing.

    A deal with no rate or no supplier amount is left out rather than guessed
    at. It cannot be matched on an amount it does not have, and including it
    would only widen the ambiguity for the deals that can.
    """
    built = []
    for order in orders:
        rate = order.supplier_rate or order.client_rate
        # The local amount is what the supplier SENDS - `supplier_pays`.
        # `supplier_receives` is the USDT, and dividing that by the rate again
        # gives a figure no payment will ever be for. This read the wrong
        # column until 5 October; `settlement.local_leg` carries the full note
        # and is the one place that decides which column this is.
        _, local = settlement.local_leg(order)
        if not rate or not local or rate <= 0:
            continue
        built.append(
            Candidate(
                key=order.display_reference,
                expected_usdt=(local / rate).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                ),
                label=order.display_reference,
            )
        )
    return built
