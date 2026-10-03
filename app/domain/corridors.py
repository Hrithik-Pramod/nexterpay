"""Where a payout lands, and what it is paid in.

NexterPay, through Jason on 3 October, with the real list. The reason this is
a module rather than a column is one fact in it:

    XOF = BJ, BF, CI, GW, ML, NE, SN, TG
    XAF = CM, CF, TD, CG, GQ, GA

Fourteen of the thirty countries here share two currencies. So a currency does
not tell you the country, and the desk's own messages prove the direction the
mapping has to run. Their settlement lines name the country:

    CI - 50250000/583 = 86,192.11   (07/09/2026) Nexterpay 5
    CM - 3000000/606  = 4,950.495   (01/09/2026) Nexterpay 1

while the rate they are priced against is quoted per currency:

    XOF: 583
    XAF: 604

Same deals, two labels. Country is the fact that is captured; currency is
derived from it; the rate is looked up by currency. Storing the currency and
trying to recover the country would be guessing between eight answers.

A rate is per currency and not per country, which is the other half of the
same point: one XOF rate covers all eight XOF countries on the same day.
"""

from __future__ import annotations

from app.domain.errors import DomainError

# Currency to the countries that use it, exactly as NexterPay sent it.
#
# Kept in this direction because that is the direction it was given in and the
# direction a person checks it in - "which countries are XOF?" is the question
# somebody asks. `COUNTRY_CURRENCY` below is derived from it so the two cannot
# drift apart.
CURRENCY_COUNTRIES: dict[str, tuple[str, ...]] = {
    "NGN": ("NG",),
    "KES": ("KE",),
    "GHS": ("GH",),
    "TZS": ("TZ",),
    "GMD": ("GM",),
    "UGX": ("UG",),
    "ZAR": ("ZA",),
    "XOF": ("BJ", "BF", "CI", "GW", "ML", "NE", "SN", "TG"),
    "XAF": ("CM", "CF", "TD", "CG", "GQ", "GA"),
    "CDF": ("CD",),
    "RWF": ("RW",),
    "ZWG": ("ZW",),
    "GNF": ("GN",),
    "SLE": ("SL",),
    "PKR": ("PK",),
    "BDT": ("BD",),
    "PHP": ("PH",),
}

# Derived, never hand-written. A country belongs to exactly one currency here,
# and `test_corridors` asserts that rather than trusting it - a country landing
# in two lists would silently take whichever was written last.
COUNTRY_CURRENCY: dict[str, str] = {
    country: currency
    for currency, countries in CURRENCY_COUNTRIES.items()
    for country in countries
}


class UnknownCountry(DomainError):
    """A two-letter code NexterPay does not pay into."""


def parse_country_code(text: str) -> str:
    """A two-letter country code, upper-cased and checked against the list.

    Checked rather than merely formatted, which is the difference that
    matters: an unrecognised country has no currency, so a deal built on one
    would be priced against nothing. Better to refuse at the point somebody
    types it than to carry it to a settlement line.
    """
    cleaned = (text or "").strip().upper()
    if not cleaned:
        raise UnknownCountry(
            "Which country? Two letters, like CI or NG."
        )
    if cleaned not in COUNTRY_CURRENCY:
        raise UnknownCountry(
            f"“{text.strip()}” is not a country NexterPay pays into. "
            f"Two letters - CI, SN, CM, NG and so on."
        )
    return cleaned


def currency_for(country_code: str) -> str:
    """The currency a payout in this country is made in."""
    return COUNTRY_CURRENCY[parse_country_code(country_code)]


def countries_for(currency_code: str) -> tuple[str, ...]:
    """Every country on this currency. Empty for one we do not serve."""
    return CURRENCY_COUNTRIES.get((currency_code or "").strip().upper(), ())


def is_shared(currency_code: str) -> bool:
    """Does this currency cover more than one country?

    True for XOF and XAF and nothing else today. Worth having as a question
    the code can ask, because every place that must insist on a country rather
    than accepting a currency is a place this is the reason.
    """
    return len(countries_for(currency_code)) > 1


__all__ = [
    "COUNTRY_CURRENCY",
    "CURRENCY_COUNTRIES",
    "UnknownCountry",
    "countries_for",
    "currency_for",
    "is_shared",
    "parse_country_code",
]
