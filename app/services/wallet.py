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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

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
    "MATCH_FRACTION",
    "USDT_DECIMALS",
    "Candidate",
    "ChainClient",
    "IncomingPayment",
    "Proposal",
    "from_micro_usdt",
    "match_all",
    "match_payment",
    "tolerance_for",
]
