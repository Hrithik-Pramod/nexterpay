"""A transaction reference pasted with no command.

NexterPay, 14 September: clients paste a reference into their group and wait,
and Gavin ends up running the lookup on their behalf. The bot should notice and
answer with the format — not with the answer, because it has no access to their
order data and never will.

    Please resubmit using the correct search format - /orderstatus <transaction ID>

Two things make this riskier than it looks, and both have a test here.

The bot only receives a plain message in a counterparty group when it is an
administrator — privacy mode otherwise withholds it. So this is being switched
on group by group, and in a group not yet promoted the feature is *silent*
rather than broken. That is worth knowing before somebody reports it as a bug.

And the pattern lives one character away from a false positive. A settlement
hash is 64 hexadecimal characters; a reference is 32. FX tickets are now full
of hashes, and without a word boundary the bot would read the first 32
characters of every one of them as a reference and correct a client who had
done nothing wrong. `test_a_settlement_hash_is_not_a_reference` uses the actual
hash from the FX test run.
"""

from __future__ import annotations

import ast
import inspect

from app.bot.handlers import client

# Jason's own example, 14 September.
UUID = "1f350f5f-9e88-4dc3-bc01-a70e3c400af5"
HEX32 = "1f350f5f9e884dc3bc01a70e3c400af5"
# From the FX settlement driven through Telegram on the 13th.
HASH64 = "6f1d8c0a3b5e7f2941a8c6d0b3e5f7a9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f1"


# --------------------------------------------------------------------------
# Finding the reference
# --------------------------------------------------------------------------

def test_a_uuid_is_found() -> None:
    assert client.transaction_ids(f"any update on {UUID} please") == [UUID]


def test_thirty_two_hex_characters_are_found() -> None:
    assert client.transaction_ids(f"ref {HEX32}") == [HEX32]


def test_several_in_one_message_are_all_found() -> None:
    """NexterPay asked for "one or multiple". A client chasing three payments
    pastes three references, and finding one of them is not finding them."""
    found = client.transaction_ids(f"{UUID} and {HEX32} and {UUID}")
    assert len(found) == 3


def test_a_settlement_hash_is_not_a_reference() -> None:
    """The near-miss that would make this feature a nuisance.

    64 hex characters, of which the first 32 look exactly like a reference.
    The word boundary is the only thing standing between this and the bot
    correcting a client who quoted the hash we sent them.
    """
    assert client.transaction_ids(HASH64) == []
    assert client.transaction_ids(f"received, thanks — {HASH64}") == []


def test_a_long_run_of_digits_is_a_reference() -> None:
    """NexterPay, 16 September: "Any number with 10 digits or more, without -
    separation"."""
    assert client.transaction_ids("1234567890") == ["1234567890"]
    assert client.transaction_ids("ref 90000000001234 please") == ["90000000001234"]


def test_nine_digits_is_not() -> None:
    """Ten was the number they gave. Nine is an invoice number, an amount, or a
    date somebody typed without slashes."""
    assert client.transaction_ids("123456789") == []


def test_a_separated_number_is_not() -> None:
    """"without - separation" — a number with anything between the groups is
    somebody writing a figure, not quoting a reference."""
    assert client.transaction_ids("1234-567-890") == []
    assert client.transaction_ids("9,000,000,000") == []


def test_the_digits_rule_is_known_to_be_loose() -> None:
    """Honest about the cost rather than quiet about it.

    Ten digits with nothing between them is also a phone number, and an amount
    typed without separators. This test does not assert the bot is right — it
    asserts that we know, so nobody later reads a false positive as a bug in
    the pattern rather than a consequence of the rule.
    """
    assert client.transaction_ids("9000000000") == ["9000000000"]


def test_a_uuid_is_not_counted_twice() -> None:
    """The digits rule could match inside a UUID. The scan resumes after each
    match, so it does not."""
    assert client.transaction_ids(UUID) == [UUID]


def test_ordinary_words_are_not_references() -> None:
    for text in (
        "any update on this please?",
        "thanks",
        "",
        "invoice 2041 is still unpaid",
        "deadbeef",                      # hex, but far too short
    ):
        assert client.transaction_ids(text) == [], text


# --------------------------------------------------------------------------
# When to speak
# --------------------------------------------------------------------------

def test_a_bare_reference_is_nudged() -> None:
    assert client.should_nudge(UUID, is_reply=False) is True


def test_somebody_who_used_the_command_is_left_alone() -> None:
    """They got it right. Correcting them would be worse than saying nothing."""
    assert client.should_nudge(
        f"/orderstatus {UUID}", is_reply=False
    ) is False


def test_the_command_is_recognised_whatever_the_case() -> None:
    assert client.should_nudge(f"/OrderStatus {UUID}", is_reply=False) is False


def test_a_reply_is_left_alone() -> None:
    """A reply in a counterparty group is traffic on a live request, and a
    reference quoted inside that conversation is ordinary.

    This rule is ours rather than NexterPay's — they specified "sent without
    /orderstatus" and said nothing about replies — so if it turns out to be
    wrong, this is the test that should change and it should change loudly.
    """
    assert client.should_nudge(UUID, is_reply=True) is False


def test_ordinary_conversation_is_left_alone() -> None:
    """The whole risk of the bot becoming an administrator everywhere: it now
    sees every word said in a client's group."""
    for text in ("morning", "any news?", "thanks, appreciated", None, "   "):
        assert client.should_nudge(text, is_reply=False) is False, text


def test_a_hash_in_conversation_is_left_alone() -> None:
    assert client.should_nudge(f"got it — {HASH64}", is_reply=False) is False


# --------------------------------------------------------------------------
# The words themselves
# --------------------------------------------------------------------------

def test_the_wording_is_theirs() -> None:
    """Quoted from NexterPay, 14 September, and not tidied."""
    assert client.NUDGE == (
        "Please resubmit using the correct search format - "
        "/orderstatus <transaction ID>"
    )


def test_the_lookup_command_is_not_one_of_ours() -> None:
    """`/orderstatus` is answered on NexterPay's side.

    If this platform ever grows a command by that name it would have to be
    np-prefixed like every other, and this message would then be pointing
    somewhere real — worth noticing rather than discovering.
    """
    from app.bot import commands as cmd

    assert client.LOOKUP_COMMAND.lstrip("/") not in cmd.ALL


# --------------------------------------------------------------------------
# Wiring — the part no unit test can see
# --------------------------------------------------------------------------

def _handler_order() -> list[str]:
    return [h.callback.__name__ for h in client.router.message.handlers]


def test_the_nudge_is_offered_the_message_before_the_catch_all() -> None:
    """Order is the whole feature.

    `client_reply` matches every group message. Registered first, it would
    resolve a pasted reference to nothing, stay silent, and this would never
    run — working perfectly in tests and never once in Telegram. That is the
    exact shape of the three bugs this project has already shipped.
    """
    order = _handler_order()
    assert "offer_lookup_format" in order, "the handler is not registered at all"
    assert "client_reply" in order
    assert order.index("offer_lookup_format") < order.index("client_reply")


def test_not_nudging_hands_the_message_on_rather_than_eating_it() -> None:
    """The regression that would be far worse than the feature is good.

    Every path that does not nudge must raise SkipHandler. A bare `return`
    would consume the update, and every ordinary client reply — the ones that
    carry real conversation into real tickets — would silently stop arriving.
    """
    tree = ast.parse(inspect.getsource(client))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "offer_lookup_format"
        ):
            bare = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Return) and n.value is None
            ]
            assert not bare, (
                "offer_lookup_format returns instead of skipping, which would "
                "swallow ordinary client messages"
            )
            skips = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Raise)
                and "SkipHandler" in ast.unparse(n)
            ]
            assert len(skips) >= 2, (
                "both the not-a-reference path and the unknown-chat path have "
                "to hand the message on"
            )
            return
    raise AssertionError("offer_lookup_format not found")


def test_the_decision_is_callable_from_a_test() -> None:
    """`should_nudge` is pulled out of the handler on purpose.

    An FSM handler cannot be called from a test, and that gap is where this
    project's last four bugs lived. If somebody inlines this decision back into
    the handler, every test above stops testing the thing that runs.
    """
    source = inspect.getsource(client.offer_lookup_format)
    assert "should_nudge(" in source
