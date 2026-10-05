"""Reading the desk's conversation, without joining in.

The FX head's complaint, through Jason on 2 October:

    the boss thinks its too many stages, he wants to replicate what he likes
    to do but automate the other to lower his pain

and, asked how much the platform should interrupt him, on 4 October:

    think working along side him

That second answer is the whole design. A colleague keeping the ledger while
he works does not interrupt the call he is on; they listen, write things down,
and are ready when he turns round. So **this module never produces anything a
counterparty sees.** Everything it notices surfaces in the Operations Group,
where the desk looks, and nowhere else.

It also never acts. Everything here returns an observation - what the message
appears to contain - and a person decides. Jason asked for this to be primary
rather than automatic, and those are different things: primary means he should
not have to drive the platform, not that the platform should commit NexterPay
to figures nobody has read.

Everything is recognised with parsers this project already has and has already
tested against their real traffic. Nothing here guesses at a new format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain import corridors, settlement_text

# A rate as the supplier gives it: a currency, then a number, and no division.
#
# From their own chat on 1 September - "XOF: 583 ✅", "XAF: 604", "XOF: 584" -
# and the division is what tells a rate from a settlement line. `XOF: 583` is
# a price; `XOF: 20100000/585` is a payment, and the settlement parser owns
# that one.
RATE_LINE = re.compile(
    r"^\s*(?P<currency>[A-Za-z]{3})\s*[:\-–]\s*(?P<rate>\d+(?:[.,]\d+)?)\s*[^\d/]*$"
)

# A rate is a local currency per 1 USDT. Theirs run from about 89 to 650, and
# the bound is deliberately generous in both directions: this decides whether
# to mention something to the desk, not whether to act on it, and a currency
# nobody has quoted yet should not be silently ignored because it is unusual.
RATE_MIN = Decimal("0.01")
RATE_MAX = Decimal("1000000")


@dataclass(frozen=True)
class Observation:
    """Something the desk's conversation appears to contain.

    `summary` is what a person reads. `payload` is what the handler needs to
    act on it if they say yes. Nothing here is a decision.
    """

    kind: str
    summary: str
    payload: dict = field(default_factory=dict)


def _rate_in(line: str) -> tuple[str, Decimal] | None:
    match = RATE_LINE.match(line or "")
    if match is None:
        return None

    currency = match.group("currency").upper()
    if not corridors.countries_for(currency):
        return None

    raw = match.group("rate").replace(",", ".")
    try:
        rate = Decimal(raw)
    except Exception:
        return None
    if not (RATE_MIN <= rate <= RATE_MAX):
        return None
    return currency, rate


def observe(text: str | None) -> list[Observation]:
    """What this message appears to contain, in the order it was written.

    Returns nothing for the overwhelming majority of messages, which is the
    point. "Morning team", "Sharing shortly" and "Any updates on rates" are
    all real messages from their chat and none of them is an event.

    A settlement and a rate are not mutually exclusive, but in practice a
    block that parses as a settlement is not also a rate quote - the division
    in every settlement line is what keeps them apart.
    """
    body = (text or "").strip()
    if not body:
        return []

    found: list[Observation] = []

    try:
        settlement = settlement_text.parse(body)
    except settlement_text.SettlementTextError:
        settlement = None

    if settlement is not None:
        total = sum(
            (line.computed_usdt for line in settlement.lines), Decimal(0)
        )
        found.append(
            Observation(
                kind="settlement",
                summary=(
                    f"{len(settlement.lines)} settlement line"
                    f"{'' if len(settlement.lines) == 1 else 's'}, "
                    f"{total:,f} USDT"
                    + (" — with a hash" if settlement.tx_hash else "")
                ),
                payload={"text": body},
            )
        )
        return found

    for line in body.splitlines():
        rate = _rate_in(line)
        if rate is None:
            continue
        currency, value = rate
        found.append(
            Observation(
                kind="rate",
                summary=f"a rate for {currency} of {value:,f}",
                payload={"currency": currency, "rate": str(value)},
            )
        )

    return found


__all__ = ["RATE_LINE", "RATE_MAX", "RATE_MIN", "Observation", "observe"]
