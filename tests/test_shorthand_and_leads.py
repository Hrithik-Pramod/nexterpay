"""Naming a counterparty mid-sentence, and the person a desk always asks.

Both from Jason on 3 October:

    ideally when he is messaging a group he can use @BBS or @ACME

    also think, there will be key people in some group he always ask, so we
    can use lead to identify

The first became `#BBS` rather than `@BBS`. Telegram clients auto-link
anything shaped like `@word` into a username, so `@BBS` would render as a
tappable link to an account that does not exist - every time, in every group,
for ever. Raised as a detail worth settling before anybody got used to it;
Jason picked `#`.

The second is a smaller change than it sounds. Leads already existed per
group. What the table could not say was which of them is *the* one, so every
time the platform wanted to address somebody it offered a list.
"""

from __future__ import annotations

import pytest

from app.bot.registry import (
    leads_for,
    preferred_lead_for,
    remove_group_lead,
    set_group_lead,
    set_preferred_lead,
)
from app.domain import shorthand

# --------------------------------------------------------------------------
# #BBS
# --------------------------------------------------------------------------

def test_a_code_is_found_mid_sentence():
    assert shorthand.find_codes("can #BBS do 611 for XOF") == ["BBS"]


def test_codes_are_upper_cased():
    assert shorthand.find_codes("#bbs and #Acme") == ["BBS", "ACME"]


@pytest.mark.parametrize(
    "written",
    ["#BBS.", "#BBS,", "(#BBS)", "#BBS?", "ask #BBS", "#BBS\nnext line"],
)
def test_punctuation_around_a_code_does_not_break_it(written):
    assert "BBS" in shorthand.find_codes(written)


def test_a_code_is_never_silently_truncated():
    """`#BBSX` must not come back as BBS.

    Half a code resolving to the wrong counterparty is worse than not
    resolving at all - it would attach a price to somebody else's deal. BBSX
    is a perfectly well-formed code that happens not to exist, so it is read
    whole and fails to resolve, which is the harmless failure.
    """
    assert shorthand.find_codes("#BBSX") == ["BBSX"]
    assert shorthand.find_codes("#BBSXY") == [], "five letters is not a code"


def test_a_hash_in_the_middle_of_a_word_is_not_a_code():
    assert shorthand.find_codes("abc#BBS") == []


def test_a_bare_hashtag_of_the_wrong_shape_is_ignored():
    assert shorthand.find_codes("#A") == []
    assert shorthand.find_codes("#TOOLONG") == []


def test_repeated_mentions_are_kept_but_can_be_collapsed():
    text = "#BBS quoted 611, #BBS can do 612"
    assert shorthand.find_codes(text) == ["BBS", "BBS"]
    assert shorthand.unique_codes(text) == ["BBS"]


def test_several_counterparties_keep_their_order():
    assert shorthand.unique_codes("#ACME wants a rate, ask #BBS") == [
        "ACME", "BBS"
    ]


def test_the_shorthand_is_stripped_before_a_message_is_relayed():
    """A supplier reading `#ACME` has just learned the client's code, which is
    the beginning of working out what NexterPay make on them."""
    assert shorthand.strip_codes("can #BBS do 611 for #ACME") == "can do 611 for"


def test_stripping_leaves_ordinary_text_alone():
    assert shorthand.strip_codes("rate on XOF is 583") == "rate on XOF is 583"


def test_nothing_is_not_an_error():
    assert shorthand.find_codes("") == []
    assert shorthand.find_codes(None) == []


# --------------------------------------------------------------------------
# The person this desk always asks
# --------------------------------------------------------------------------

async def test_no_preference_means_no_answer(session, pexi_supplier):
    """Rather than falling back to any lead at all.

    The question is "who do we always ask". When nobody has said, the answer
    is not a name picked off a list.
    """
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    assert await preferred_lead_for(session, pexi_supplier) is None


async def test_a_preferred_lead_is_returned(session, pexi_supplier):
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_preferred_lead(session, pexi_supplier, 900)

    lead = await preferred_lead_for(session, pexi_supplier)
    assert lead.display_name == "Marco"


async def test_a_currency_preference_wins_over_the_general_one(
    session, pexi_supplier
):
    """"For XOF I always ask Marco" has to beat "generally ask Amina", or
    setting the narrower one means nothing."""
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_group_lead(session, pexi_supplier, telegram_user_id=901, display_name="Amina")
    await set_preferred_lead(session, pexi_supplier, 901)
    await set_preferred_lead(session, pexi_supplier, 900, currency="XOF")

    assert (await preferred_lead_for(
        session, pexi_supplier, currency="XOF"
    )).display_name == "Marco"
    assert (await preferred_lead_for(
        session, pexi_supplier
    )).display_name == "Amina"


async def test_a_currency_with_no_preference_falls_back_to_the_general_one(
    session, pexi_supplier
):
    await set_group_lead(session, pexi_supplier, telegram_user_id=901, display_name="Amina")
    await set_preferred_lead(session, pexi_supplier, 901)

    assert (await preferred_lead_for(
        session, pexi_supplier, currency="NGN"
    )).display_name == "Amina"


async def test_setting_a_currency_preference_leaves_the_general_one_alone(
    session, pexi_supplier
):
    """Otherwise "always ask Marco for XOF" would quietly unseat "always ask
    Amina" for everything else, which is not what anybody meant."""
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_group_lead(session, pexi_supplier, telegram_user_id=901, display_name="Amina")
    await set_preferred_lead(session, pexi_supplier, 901)
    await set_preferred_lead(session, pexi_supplier, 900, currency="XOF")

    assert await preferred_lead_for(session, pexi_supplier) is not None


async def test_a_new_general_preference_replaces_the_old_one(
    session, pexi_supplier
):
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_group_lead(session, pexi_supplier, telegram_user_id=901, display_name="Amina")
    await set_preferred_lead(session, pexi_supplier, 900)
    await set_preferred_lead(session, pexi_supplier, 901)

    assert (await preferred_lead_for(
        session, pexi_supplier
    )).display_name == "Amina"
    assert len(await leads_for(session, pexi_supplier)) == 2, (
        "replacing a preference must not remove anybody from the group"
    )


async def test_somebody_who_is_not_a_lead_cannot_be_preferred(
    session, pexi_supplier
):
    assert await set_preferred_lead(session, pexi_supplier, 999) is None


async def test_removing_a_lead_removes_the_preference_with_them(
    session, pexi_supplier
):
    """A preference pointing at somebody who has left the group would offer
    to address a person who is no longer there."""
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_preferred_lead(session, pexi_supplier, 900)
    await remove_group_lead(session, pexi_supplier, 900)

    assert await preferred_lead_for(session, pexi_supplier) is None


async def test_the_currency_is_read_however_it_is_typed(session, pexi_supplier):
    await set_group_lead(session, pexi_supplier, telegram_user_id=900, display_name="Marco")
    await set_preferred_lead(session, pexi_supplier, 900, currency="xof")

    assert await preferred_lead_for(session, pexi_supplier, currency="XOF")
    assert await preferred_lead_for(session, pexi_supplier, currency=" xof ")


# --------------------------------------------------------------------------
# The shorthand never leaves the building
# --------------------------------------------------------------------------

def test_a_staff_reply_strips_the_shorthand():
    """A supplier reading `#ACME` has just learned the client's code, which is
    the beginning of working out what NexterPay make on them.

    The same leak `supplier_reference` exists to prevent, arriving through a
    different door - and a door that only opened once the desk was given a
    reason to write counterparty codes in their messages.
    """
    from app.services.relay import staff_reply_text

    out = staff_reply_text("ACME-1042", "can #BBS do 611 for #ACME")

    assert "#BBS" not in out
    assert "#ACME" not in out
    assert "611" in out


def test_an_outbound_request_strips_it_too():
    from app.services.relay import outbound_body

    out = outbound_body("Rate for XOF", "Rate for XOF\nasking #BBS today")

    assert "#BBS" not in out
    assert "asking" in out


def test_the_reference_in_the_header_survives():
    """Only the hash shorthand goes. `ACME-1042` is what the counterparty is
    meant to quote back."""
    from app.services.relay import staff_reply_text

    assert "ACME-1042" in staff_reply_text("ACME-1042", "noted")


def test_ordinary_text_is_untouched():
    """A message with no shorthand in it must come out exactly as written -
    stripping is not a licence to reformat somebody's words."""
    from app.services.relay import staff_reply_text

    assert "rate on XOF is 583" in staff_reply_text("ACME-1042", "rate on XOF is 583")


def test_every_path_to_a_counterparty_strips_the_shorthand() -> None:
    """Structural, because the two functions above are not the point.

    The point is that *any* text a member of staff types which reaches a
    counterparty has the shorthand taken out of it. Both of the current paths
    do; this fails if a third appears that does not, which is how the three
    reference leaks and the three internal-request doors each happened.
    """
    import ast
    import pathlib

    source = pathlib.Path("app/services/relay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    composers = {"staff_reply_text", "outbound_body"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in composers:
            continue
        body = ast.unparse(node)
        assert "strip_codes" in body, (
            f"{node.name} composes text for a counterparty without stripping "
            f"the desk's #CODE shorthand out of it"
        )
