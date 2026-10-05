"""Reading the desk's conversation, without joining in.

The FX head, through Jason on 2 October: "the boss thinks its too many
stages, he wants to replicate what he likes to do but automate the other to
lower his pain". Asked how much the platform should interrupt him, on
4 October: "think working along side him".

Every input in this file is a real message from the supplier chat Jason
forwarded on 3 October. That matters more here than anywhere else in the
suite: the job is to recognise somebody else's conversation, and examples
written to suit the parser would only prove it agrees with itself.

**What it does not notice is the measure of it.** Their week contains perhaps
four events and several dozen messages. "Morning team", "Sharing shortly",
"Any updates on rates", "We need this settling asap please advise URGENTLY" -
all real, none of them an event, and a platform that piped up at any of them
would be worse than one he has to invoke.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import capture

THEIR_SETTLEMENT = """Hello team
XAF: 3000000/606=4 950,495
XOF: 20100000/585=34 358,974

≡ 39 309,469 USDT ✅

51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474"""

THEIR_REQUEST_LIST = """• SN - 11,064,496.85/583=18,978.554  ( 07/09/2026) Nexterpay 7
• CI - 1260148.447/583=2,161.49   ( 07/09/2026) Nexterpay 2"""


# --------------------------------------------------------------------------
# What it ignores
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "Morning Team\nHope you had a good weekend",
        "Morning team",
        "Sharing shortly",
        "Will share asap",
        "Any updates on rates Dieng\nThanks",
        "Received Dieng\nThank you",
        "We need this settling asap please advise URGENTLY",
        "All Clients are now chasing these settlements as they have fallen "
        "outside the 5 day limit",
        "Please update us on this as well so we can settle our clients today.",
        "@Nexter001 @Slimce004 please check",
        "",
        "   ",
    ],
)
def test_ordinary_traffic_is_not_an_event(message):
    assert capture.observe(message) == []


def test_a_question_about_a_rate_is_not_a_rate():
    """"XAF Dieng ?" is somebody asking. Reading it as a quote would record a
    price nobody gave."""
    assert capture.observe("XAF Dieng ?") == []


def test_nothing_is_not_an_error():
    assert capture.observe(None) == []


# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------

def test_a_rate_is_recognised():
    """Their format, tick and all."""
    found = capture.observe("XOF: 583 ✅")

    assert len(found) == 1
    assert found[0].kind == "rate"
    assert found[0].payload == {"currency": "XOF", "rate": "583"}


def test_two_rates_in_one_message_are_both_seen():
    found = capture.observe("XOF: 584\nXAF: 604")

    assert [o.payload["currency"] for o in found] == ["XOF", "XAF"]


def test_a_currency_this_desk_does_not_deal_in_is_ignored():
    """EUR is not a corridor. A platform that recorded a rate for it would be
    inventing a product."""
    assert capture.observe("EUR: 583") == []


def test_a_rate_with_a_division_is_a_settlement_line_instead():
    """The division is the whole distinction. `XOF: 583` is a price;
    `XOF: 20100000/585` is a payment."""
    found = capture.observe("XOF: 20100000/585=34 358,974")

    assert [o.kind for o in found] == ["settlement"]


# --------------------------------------------------------------------------
# Settlements
# --------------------------------------------------------------------------

def test_their_settlement_is_recognised():
    found = capture.observe(THEIR_SETTLEMENT)

    assert len(found) == 1
    assert found[0].kind == "settlement"
    assert "2 settlement lines" in found[0].summary
    assert "39,309.469" in found[0].summary
    assert "with a hash" in found[0].summary


def test_a_settlement_carries_the_block_so_nothing_is_retyped():
    """The payload is the text as it arrived, because the desk should confirm
    what the supplier wrote rather than what the platform made of it."""
    found = capture.observe(THEIR_SETTLEMENT)
    assert found[0].payload["text"].strip().startswith("Hello team")


def test_a_request_list_is_recognised_without_a_hash():
    """Sent before the money moves, and routinely. Saying "with a hash" when
    there is none would be the platform reporting a payment that has not
    happened."""
    found = capture.observe(THEIR_REQUEST_LIST)

    assert found[0].kind == "settlement"
    assert "with a hash" not in found[0].summary


def test_the_greeting_above_a_settlement_does_not_stop_it_being_one():
    """Their blocks open with "Hello team" and the parser has to see past it,
    because that is how every one of them is written."""
    assert capture.observe(THEIR_SETTLEMENT)[0].kind == "settlement"


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------

def test_nothing_here_decides_anything() -> None:
    """Jason asked for this to be primary rather than automatic, and they are
    different things. Primary means he should not have to drive the platform;
    it does not mean the platform commits NexterPay to figures nobody has
    read.

    So this module returns observations and nothing else. If it ever gains a
    session, a gateway or a write, it has stopped observing.
    """
    import inspect

    source = inspect.getsource(capture)

    for forbidden in (
        "AsyncSession", "session", "gateway", "send_message",
        "commit", "flush", "await ",
    ):
        assert forbidden not in source, (
            f"capture.py mentions {forbidden!r}. It reads messages and says "
            f"what they appear to contain - a person decides, and the handler "
            f"is where that happens."
        )


def test_a_rate_has_to_be_a_plausible_number() -> None:
    """Generous on purpose in both directions: this decides whether to mention
    something, not whether to act on it, and a currency nobody has quoted yet
    should not be ignored for being unusual."""
    assert capture.observe("XOF: 0") == []
    assert capture.observe("XOF: 89.50")[0].payload["rate"] == "89.50"
    assert Decimal(capture.observe("XOF: 650")[0].payload["rate"]) == 650
