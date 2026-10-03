"""Country decides currency, and never the other way round.

NexterPay sent the real list on 3 October. One fact in it drives this whole
module:

    XOF = BJ, BF, CI, GW, ML, NE, SN, TG
    XAF = CM, CF, TD, CG, GQ, GA

Fourteen of thirty countries share two currencies. So "the deal is in XOF"
does not tell you where the money lands, and a system that stored only the
currency would be guessing between eight answers every time it needed the
country back.

Their own messages are the evidence. Settlement lines name the country -
`CI - 50250000/583` - and rates are quoted per currency - `XOF: 583`. The
same deal wears both labels, so the mapping has to be able to get from one to
the other, in the direction that does not lose information.
"""

from __future__ import annotations

import pytest

from app.domain import corridors


def test_a_country_gives_exactly_one_currency():
    assert corridors.currency_for("CI") == "XOF"
    assert corridors.currency_for("CM") == "XAF"
    assert corridors.currency_for("NG") == "NGN"


def test_the_shared_currencies_are_the_reason_this_module_exists():
    """XOF and XAF, and the exact membership NexterPay sent."""
    assert corridors.countries_for("XOF") == (
        "BJ", "BF", "CI", "GW", "ML", "NE", "SN", "TG"
    )
    assert corridors.countries_for("XAF") == ("CM", "CF", "TD", "CG", "GQ", "GA")
    assert corridors.is_shared("XOF")
    assert corridors.is_shared("XAF")


def test_every_other_currency_is_one_country():
    """Not decoration. If a third shared currency is ever added, the places
    that quietly assume a currency implies a country need revisiting, and this
    is what will say so."""
    shared = {c for c in corridors.CURRENCY_COUNTRIES if corridors.is_shared(c)}
    assert shared == {"XOF", "XAF"}


def test_no_country_belongs_to_two_currencies():
    """The derived map would silently keep whichever was written last.

    Checked against the source list rather than the derived one, because the
    derived one cannot show the collision - that is the whole problem.
    """
    seen: dict[str, str] = {}
    for currency, countries in corridors.CURRENCY_COUNTRIES.items():
        for country in countries:
            assert country not in seen, (
                f"{country} is listed under both {seen[country]} and "
                f"{currency}. The derived mapping would keep only one."
            )
            seen[country] = currency


def test_the_derived_map_covers_the_whole_list():
    total = sum(len(c) for c in corridors.CURRENCY_COUNTRIES.values())
    assert len(corridors.COUNTRY_CURRENCY) == total


def test_the_codes_are_the_right_shape():
    for currency, countries in corridors.CURRENCY_COUNTRIES.items():
        assert len(currency) == 3 and currency.isalpha() and currency.isupper()
        for country in countries:
            assert len(country) == 2 and country.isalpha() and country.isupper()


# --------------------------------------------------------------------------
# Parsing what somebody typed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["ci", " CI ", "Ci"])
def test_a_country_is_read_however_it_is_typed(typed):
    assert corridors.parse_country_code(typed) == "CI"


def test_an_unknown_country_is_refused_rather_than_carried():
    """Refused at the point it is typed, because a country with no currency
    produces a deal priced against nothing - and the place that would notice
    is a settlement line, days later."""
    with pytest.raises(corridors.UnknownCountry):
        corridors.parse_country_code("ZZ")


def test_the_refusal_says_what_was_wanted():
    with pytest.raises(corridors.UnknownCountry) as caught:
        corridors.parse_country_code("XOF")
    assert "two letters" in str(caught.value).lower()


def test_nothing_typed_is_asked_for_rather_than_guessed():
    with pytest.raises(corridors.UnknownCountry):
        corridors.parse_country_code("")


def test_an_unserved_currency_has_no_countries():
    assert corridors.countries_for("EUR") == ()
    assert not corridors.is_shared("EUR")


# --------------------------------------------------------------------------
# The real lines
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "country, currency",
    [("CM", "XAF"), ("SN", "XOF"), ("CI", "XOF")],
)
def test_the_countries_from_their_own_settlement_lines(country, currency):
    """Taken from the Block chat of 3 October:

        CM- 3000000/606  = 4,950.495   (01/09/2026) Nexterpay 1
        SN- 20100000/585 = 34,358.974  (01/09/2026) Nexterpay 5
        CI- 50250000/583 = 86,192.11   (07/09/2026) Nexterpay 5

    and the rates they were priced against, quoted per currency in the same
    conversation: XAF 606 and 604, XOF 585 and 583.
    """
    assert corridors.currency_for(country) == currency
