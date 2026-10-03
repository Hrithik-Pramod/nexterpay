"""Reading a settlement the way NexterPay already write it.

Every input in this file is taken from their real supplier chat, forwarded by
Jason on 3 October. That matters more here than anywhere else in the suite:
this module exists to read somebody else's format, so examples invented to
suit the parser would test nothing except that it agrees with itself.

The fault this file is really guarding is one the parser had, and that only
their own data revealed. The desk writes two different decimal conventions in
the same week:

    XOF: 20100000/585 = 34 358,974     <- space groups, decimal comma
    SN - 11,064,496.85/583 = 18,978.554 <- comma groups, decimal point

and they write three decimal places about as often as three-digit thousands
groups. So once the spaces are stripped, `4950,495` and `4,950` are the same
shape, and a "groups of three means thousands" rule reads the first as four
million nine hundred and fifty thousand. Out by a thousand, silently, on
money.

The space is the only thing that distinguishes them, which is why
`test_a_space_means_the_comma_is_a_decimal_point` is the most important test
here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain import settlement_text as text

# Verbatim, including the spacing and the tick.
THEIR_FIRST = """Hello team
XAF: 3000000/606=4 950,495
XOF: 20100000/585=34 358,974

≡ 39 309,469 USDT ✅

51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474"""

THEIR_SECOND = """XOF: 50250000/583=86 192,11
XOF: 45000000/583=77 186,964

86192 + 77186 = 163 378 USDT

ba982263a27748cb69727fec6d974330a0fc5856190bfafa417d69e98ba34a4a"""

THEIR_REQUEST_LIST = """Updated settlement requests :

• SN - 11,064,496.85/583=18,978.554  ( 07/09/2026) Nexterpay 7
• SN - 3685620.874/583= 6,321.82 ( 07/09/2026) Nexterpay 5
• CI - 1260148.447/583=2,161.49   ( 07/09/2026) Nexterpay 2
• CM- 3000000/606=4,950.495    ( 01/09/2026) Nexterpay 1"""


# --------------------------------------------------------------------------
# Figures, in both of their conventions
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3000000", "3000000"),
        ("4 950,495", "4950.495"),
        ("34 358,974", "34358.974"),
        ("39 309,469", "39309.469"),
        ("86 192,11", "86192.11"),
        ("163 378", "163378"),
        ("11,064,496.85", "11064496.85"),
        ("18,978.554", "18978.554"),
        ("1260148.447", "1260148.447"),
        ("2,161.49", "2161.49"),
        ("1.234,56", "1234.56"),
    ],
)
def test_their_figures(raw, expected):
    assert text.parse_figure(raw) == Decimal(expected)


def test_a_space_means_the_comma_is_a_decimal_point():
    """The rule the whole module turns on.

    `4 950,495` groups with a space, so the comma cannot also be a grouping
    character. Without this the three-digit test reads it as 4,950,495 - the
    exact failure that was in the first version of this parser, found by
    running it over their messages.
    """
    assert text.parse_figure("4 950,495") == Decimal("4950.495")
    assert text.parse_figure("4,950") == Decimal("4950")  # no space: grouped


def test_the_last_separator_wins_when_both_appear():
    assert text.parse_figure("11,064,496.85") == Decimal("11064496.85")
    assert text.parse_figure("1.234,56") == Decimal("1234.56")


def test_a_short_group_is_a_decimal():
    assert text.parse_figure("4,95") == Decimal("4.95")


def test_nonsense_is_refused_rather_than_guessed():
    for raw in ("", "   ", "about 4m", "-5"):
        with pytest.raises(text.SettlementTextError):
            text.parse_figure(raw)


# --------------------------------------------------------------------------
# Their first settlement
# --------------------------------------------------------------------------

def test_their_first_settlement_parses_exactly():
    parsed = text.parse(THEIR_FIRST)

    assert [line.label for line in parsed.lines] == ["XAF", "XOF"]
    assert round(parsed.lines[0].computed_usdt, 3) == Decimal("4950.495")
    assert round(parsed.lines[1].computed_usdt, 3) == Decimal("34358.974")


def test_the_total_they_stated_is_read():
    parsed = text.parse(THEIR_FIRST)
    assert parsed.stated_total == Decimal("39309.469")


def test_their_first_settlement_ties_out():
    """Their arithmetic was right on this one, and the parser agrees with it.

    Worth asserting as well as the slip below: a checker that only ever finds
    problems is a checker nobody believes the day it finds a real one.
    """
    parsed = text.parse(THEIR_FIRST)
    computed = sum(line.computed_usdt for line in parsed.lines)
    assert round(parsed.stated_total - computed, 3) == Decimal("0.000")


def test_the_hash_is_found():
    parsed = text.parse(THEIR_FIRST)
    assert parsed.tx_hash == (
        "51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474"
    )


def test_a_currency_label_does_not_invent_a_country():
    """XOF names eight countries. Picking one would be making it up."""
    parsed = text.parse(THEIR_FIRST)
    assert parsed.lines[1].currency_code == "XOF"
    assert parsed.lines[1].country_code is None


# --------------------------------------------------------------------------
# Their second settlement, which was short
# --------------------------------------------------------------------------

def test_the_seventh_of_september_slip_is_visible_from_the_text_alone():
    """86192 + 77186 = 163 378, against lines totalling 163,379.07.

    Jason confirmed it was a slip. This is the whole case for the desk
    pasting the block rather than the platform trusting the total in it.
    """
    parsed = text.parse(THEIR_SECOND)
    computed = sum(line.computed_usdt for line in parsed.lines)

    assert round(computed, 2) == Decimal("163379.07")
    assert parsed.stated_total == Decimal("163378")
    assert round(parsed.stated_total - computed, 2) == Decimal("-1.07")


def test_the_total_is_read_after_the_last_equals_not_summed():
    """Their line carries its own arithmetic - `86192 + 77186 = 163 378` -
    and that arithmetic is the thing that was wrong. Reading the parts and
    adding them would reproduce the mistake instead of catching it."""
    parsed = text.parse(THEIR_SECOND)
    assert parsed.stated_total == Decimal("163378")


# --------------------------------------------------------------------------
# The request list, which is where the countries are
# --------------------------------------------------------------------------

def test_the_request_list_parses_with_countries():
    parsed = text.parse(THEIR_REQUEST_LIST)

    assert [line.label for line in parsed.lines] == ["SN", "SN", "CI", "CM"]
    assert [line.country_code for line in parsed.lines] == ["SN", "SN", "CI", "CM"]
    assert [line.currency_code for line in parsed.lines] == [
        "XOF", "XOF", "XOF", "XAF",
    ]


def test_the_nexterpay_account_is_picked_up():
    parsed = text.parse(THEIR_REQUEST_LIST)
    assert [line.account for line in parsed.lines] == ["7", "5", "2", "1"]


def test_the_stated_usdt_on_each_line_is_kept():
    """Kept rather than trusted. The platform computes its own and compares;
    holding theirs is what makes the comparison possible."""
    parsed = text.parse(THEIR_REQUEST_LIST)
    assert parsed.lines[0].stated_usdt == Decimal("18978.554")
    assert round(parsed.lines[0].computed_usdt, 3) == Decimal("18978.554")


def test_bullets_dashes_dates_and_spacing_are_all_tolerated():
    """`• CM- 3000000/606=4,950.495    ( 01/09/2026) Nexterpay 1` - every one
    of those is furniture around two numbers."""
    parsed = text.parse(THEIR_REQUEST_LIST)
    last = parsed.lines[3]
    assert last.local_amount == Decimal("3000000")
    assert last.rate == Decimal("606")


def test_a_list_with_no_payment_yet_is_still_readable():
    """The desk pastes the request before the money moves. No hash and no
    total is a normal state, not a failure."""
    parsed = text.parse(THEIR_REQUEST_LIST)
    assert parsed.tx_hash is None
    assert parsed.stated_total is None


# --------------------------------------------------------------------------
# What is not a settlement line
# --------------------------------------------------------------------------

def test_conversation_around_the_figures_is_ignored():
    parsed = text.parse(
        "Morning team\n"
        "Hope you had a good weekend\n"
        "XAF: 3000000/606=4 950,495\n"
        "Thank you"
    )
    assert len(parsed.lines) == 1


def test_a_block_with_nothing_in_it_is_refused_with_an_example():
    with pytest.raises(text.SettlementTextError) as caught:
        text.parse("Morning team\nAny updates on rates please")
    assert "/" in str(caught.value)


def test_an_unknown_label_is_not_a_line():
    assert text.parse_line("ZZ: 3000000/606=4950") is None


def test_a_zero_rate_is_not_a_line():
    """Dividing by it is the next thing that would happen."""
    assert text.parse_line("CI: 3000000/0=4950") is None
