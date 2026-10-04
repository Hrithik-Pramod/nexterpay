"""Reading a settlement the way NexterPay already write it.

The desk does not want a wizard. They send and receive settlements as a block
of text today, and the fastest way to record one is to paste the block they
were already sent. So this module reads their format rather than asking them
to learn ours.

Everything here is drawn from a week of their real supplier chat, forwarded by
Jason on 3 October. Both of these are theirs, verbatim:

    XAF: 3000000/606=4 950,495
    XOF: 20100000/585=34 358,974

    ≡ 39 309,469 USDT ✅

    51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474

and

    • SN - 11,064,496.85/583=18,978.554  ( 07/09/2026) Nexterpay 7
    • CI - 1260148.447/583=2,161.49      ( 07/09/2026) Nexterpay 2

**The two blocks do not agree on what a comma means.** `34 358,974` is thirty
four thousand with a decimal comma; `18,978.554` is eighteen thousand with a
decimal point. Both were written by the same desk in the same week. A parser
that picks one convention reads half their messages wrong by a factor of a
thousand, silently, on figures that are money - so the disambiguation in
`parse_figure` is the most load-bearing thing in this file, and it is the
reason this is a module with its own tests rather than a regex in a handler.

The label is read as a country where it can be - their lines carry CM, SN, CI -
and as a currency otherwise, since the older block uses XAF and XOF for the
same deals. Both are kept as written and resolved later, because `XOF` names
eight countries and guessing one would be inventing a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.domain import corridors
from app.domain.errors import DomainError

# Tron transaction ids: 64 hex characters, on a line of their own in every
# settlement they have sent.
HASH_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")

# A settlement line. Deliberately loose about the furniture around the
# figures - bullets, dashes, colons, the date in brackets, the Nexterpay
# account - and strict about the two numbers that matter.
LINE_RE = re.compile(
    r"""
    ^\s*[•\-*]?\s*                      # bullet, if any
    (?P<label>[A-Za-z]{2,3})            # CM, SN, CI - or XAF, XOF
    \s*[-:–]?\s*                        # the separator, in its many moods
    (?P<local>[\d][\d\s,.]*)            # the local amount
    \s*/\s*
    (?P<rate>[\d][\d\s,.]*?)            # the rate
    (?:\s*=\s*(?P<usdt>[\d][\d\s,.]*?))?  # what they made it, if stated
    (?:\s*\(\s*(?P<date>[\d/.\-]+)\s*\))?  # the date, if stated
    (?:\s*Nexterpay\s*(?P<account>\w+))?   # the account, if stated
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


class SettlementTextError(DomainError):
    """A pasted settlement that could not be read."""


@dataclass(frozen=True)
class ParsedLine:
    """One line of a pasted settlement, before it is matched to an order."""

    label: str
    local_amount: Decimal
    rate: Decimal
    stated_usdt: Decimal | None = None
    account: str | None = None

    @property
    def is_country(self) -> bool:
        return self.label in corridors.COUNTRY_CURRENCY

    @property
    def country_code(self) -> str | None:
        """The country, when the line named one.

        `None` for a line labelled XOF, because XOF is eight countries and
        picking one would be inventing a fact. The handler asks instead.
        """
        return self.label if self.is_country else None

    @property
    def currency_code(self) -> str | None:
        if self.is_country:
            return corridors.currency_for(self.label)
        return self.label if corridors.countries_for(self.label) else None

    @property
    def computed_usdt(self) -> Decimal:
        # Six places, because that is USDT's precision on Tron. An unrounded
        # division recurs and carries twenty-eight digits into whatever
        # renders it.
        return (self.local_amount / self.rate).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )


@dataclass(frozen=True)
class ParsedSettlement:
    lines: list[ParsedLine]
    tx_hash: str | None
    stated_total: Decimal | None


def parse_figure(text: str) -> Decimal:
    """A number as this desk writes it, in either of their two conventions.

    The rules, in the order they are applied:

    * A space between digits is only ever a thousands separator. Nobody
      writes a decimal space.
    * **A number that groups with spaces therefore uses a comma as its
      decimal point.** `4 950,495` is four thousand nine hundred and fifty
      point four nine five, and this rule is the only thing that says so.
    * If both a comma and a dot appear, the **last one** is the decimal
      separator. That reads `11,064,496.85` and `1.234,56` correctly without
      either convention having to be declared.
    * If only commas appear and nothing grouped with spaces, they are
      thousands separators when every group after the first is exactly three
      digits, and a decimal comma otherwise. So `4,950` is four thousand nine
      hundred and fifty, and `4,95` is four point nine five.

    The space rule is not decoration. Without it `4 950,495` reads as four
    million nine hundred and fifty thousand - out by a factor of a thousand,
    silently, on a figure that is money. The three-digit test cannot catch it
    because this desk writes three decimal places as often as three-digit
    thousands groups, so the two forms are identical once the space is gone.
    That was found by running this parser over their real messages rather
    than over examples written to suit it.

    The last rule is the one place this module can still be wrong, and it is
    a judgement: a bare `4,950` meant as four point nine five would be read
    as four thousand nine hundred and fifty. It has never appeared in their
    traffic, and every genuinely ambiguous figure they have sent carries
    either a space group or a dot alongside.
    """
    raw = (text or "").strip()
    if not raw:
        raise SettlementTextError("There is no number here.")

    # Checked before the spaces are removed, because removing them is what
    # destroys the evidence.
    grouped_by_spaces = re.search(r"\d[\s  ]+\d", raw) is not None

    cleaned = re.sub(r"[\s  ]", "", raw)

    has_comma = "," in cleaned
    has_dot = "." in cleaned

    if has_comma and has_dot:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma:
        if grouped_by_spaces:
            # The spaces were the thousands separator, so the comma is not.
            cleaned = cleaned.replace(",", ".")
        else:
            groups = cleaned.split(",")
            looks_grouped = len(groups) > 1 and all(
                len(group) == 3 and group.isdigit() for group in groups[1:]
            )
            cleaned = cleaned.replace(",", "" if looks_grouped else ".")

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise SettlementTextError(f"“{raw}” is not a number.") from None
    if value < 0:
        raise SettlementTextError(f"“{raw}” is negative.")
    return value


def parse_line(text: str) -> ParsedLine | None:
    """One line, or None if it is not a settlement line at all.

    None rather than an error, because a pasted block is full of lines that
    are not settlement lines - greetings, the total, the hash, blank space -
    and the caller sorts them out.
    """
    match = LINE_RE.match(text or "")
    if match is None:
        return None

    label = match.group("label").upper()
    if not (label in corridors.COUNTRY_CURRENCY or corridors.countries_for(label)):
        return None

    try:
        local = parse_figure(match.group("local"))
        rate = parse_figure(match.group("rate"))
    except SettlementTextError:
        return None
    if rate == 0:
        return None

    stated = match.group("usdt")
    try:
        stated_usdt = parse_figure(stated) if stated else None
    except SettlementTextError:
        stated_usdt = None

    return ParsedLine(
        label=label,
        local_amount=local,
        rate=rate,
        stated_usdt=stated_usdt,
        account=match.group("account"),
    )


def find_hash(text: str) -> str | None:
    match = HASH_RE.search(text or "")
    return match.group(1).lower() if match else None


def find_total(text: str, lines: list[ParsedLine]) -> Decimal | None:
    """The figure the desk says was actually paid.

    Their two formats both exist:

        ≡ 39 309,469 USDT ✅
        86192 + 77186 = 163 378 USDT

    so the total is taken as the last number on a line that mentions USDT and
    is not itself a settlement line. The second form carries its own
    arithmetic, which is exactly the arithmetic that was wrong on 7 September,
    so only the figure after the final `=` is read - never the sum of the
    parts, which would reproduce the mistake rather than catch it.
    """
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if "usdt" not in line.lower():
            continue
        if parse_line(line) is not None:
            continue
        candidate = line.split("=")[-1]
        numbers = re.findall(r"[\d][\d\s,.]*", candidate)
        if not numbers:
            continue
        try:
            return parse_figure(numbers[-1])
        except SettlementTextError:
            continue
    return None


def parse(text: str) -> ParsedSettlement:
    """A whole pasted block.

    Refuses only when there is nothing to work with. A block with lines but no
    hash is perfectly normal - the desk often pastes the request before the
    payment has been made - and a block with no total is read as a settlement
    whose payment figure is not yet known.
    """
    lines = [
        parsed
        for raw in (text or "").splitlines()
        if (parsed := parse_line(raw)) is not None
    ]
    if not lines:
        raise SettlementTextError(
            "No settlement lines found. Each line should read like "
            "“CI 50250000/583” — country, amount, and the rate it was "
            "converted at."
        )
    return ParsedSettlement(
        lines=lines,
        tx_hash=find_hash(text),
        stated_total=find_total(text, lines),
    )


__all__ = [
    "ParsedLine",
    "ParsedSettlement",
    "SettlementTextError",
    "find_hash",
    "find_total",
    "parse",
    "parse_figure",
    "parse_line",
]
