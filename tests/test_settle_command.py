"""Matching a pasted settlement to the deals it is about.

Their lines carry no reference:

    CI - 50250000/583 = 86,192.11   (07/09/2026) Nexterpay 5

so the only things to match on are the currency and the amount the supplier is
sending. That works, and the interesting part is where it must refuse to.

Two clients sending fifty million XOF on the same day is not unusual on a desk
doing volume, and guessing between them attaches a payment to the wrong
client's deal. That is not a cosmetic error: one client is told their money
has arrived when it has not, and another is left waiting while the platform
insists they were paid. So an ambiguous line is carried as a problem rather
than resolved, and the desk is shown both candidates.

A line that cannot be matched is never dropped. Three of four lines understood
and the fourth silently gone is the worst outcome available here - the total
looks plausible, and a deal nobody recorded is waiting on money that the
platform thinks has been accounted for.
"""

from __future__ import annotations

from decimal import Decimal

from app.bot.handlers import fx as handlers
from app.db.models import Client
from app.domain import fx, settlement, settlement_text
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor


async def _awaiting(
    session, acme_support, operator, *, currency, supplier_sends,
    rate="585", country=None, subject="deal",
):
    """A deal waiting on settlement, for the amount a supplier will send.

    Built through `fx.create_supplier_order` rather than by assigning columns,
    and that is the whole point of this helper.

    It used to set `order.supplier_receives = <the local amount>` directly.
    That is an order the platform cannot produce - the order flow puts the
    local amount in `supplier_pays` and the USDT in `supplier_receives` - and
    because the matcher happened to read the same wrong column, every test
    here passed while the feature could not match a single real deal. Found on
    5 October by driving one deal through the platform end to end.

    The rule this encodes: a fixture that writes columns a flow would never
    write together is not a shortcut, it is a second implementation of the
    flow, and the suite then tests the two copies against each other.
    """
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject=subject,
        original_message="Please provide a rate.",
        raised_by_name="Gavs D",
    )
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    order.currency_code = currency
    order.country_code = country
    order.status = FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE
    await session.flush()

    sends = Decimal(supplier_sends)
    supplier_rate = Decimal(rate)
    await fx.create_supplier_order(
        session, order,
        account_name="Nexterpay 5",
        rate=supplier_rate,
        pays=sends,
        pays_currency=currency,
        receives=(sends / supplier_rate).quantize(Decimal("0.000001")),
        receives_currency="USDT",
        actor=Actor.of(operator),
    )
    order.status = FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()
    return order


THEIR_BLOCK = """XAF: 3000000/606=4 950,495
XOF: 20100000/585=34 358,974

≡ 39 309,469 USDT ✅

51c86654d87af90109a33bead642ca329771a4669d4bdc749219a49b78d80474"""


# --------------------------------------------------------------------------
# The column the amount lives in
# --------------------------------------------------------------------------
#
# Found live on 5 October, and the most expensive kind of fault this project
# produces: everything passed, and the feature could not match a single deal
# the platform itself had made.

async def test_a_deal_built_by_the_platform_is_matched_by_its_own_settlement(
    session, acme_support, support_ops, operator
):
    """The one test that would have caught it.

    `_awaiting` goes through `fx.create_supplier_order`, so the order here has
    its columns where the order flow puts them: the local amount in
    `supplier_pays`, the USDT in `supplier_receives`. The matcher read the
    second one, compared XOF against USDT, and found nothing - for every deal,
    every time.

    Deliberately asserts the columns too. Matching correctly for the wrong
    reason is how this survived a fortnight.
    """
    deal = await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20130000", rate="585", country="SN",
    )

    assert deal.supplier_pays == Decimal("20130000"), (
        "the local amount belongs in supplier_pays - this is the fixture "
        "lying if it fails"
    )
    assert deal.supplier_receives_currency == "USDT"

    parsed = settlement_text.parse("XOF: 20130000/585")
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched, (
        "a settlement line for exactly this order found no open deal. The "
        "matcher is reading a different column from the one the order flow "
        "writes."
    )
    assert matches[0].order.id == deal.id


def test_only_one_place_decides_which_column_holds_the_local_amount():
    """`settlement.local_leg`, and nothing else.

    Two modules worked this out independently - the settlement matcher and the
    wallet matcher - and both got it wrong in the same way. A second copy of a
    decision is a second chance to make it differently.
    """
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.as_posix().endswith("app/domain/settlement.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if ".supplier_receives" in line and "_currency" not in line:
                offenders.append(f"{path.as_posix()}:{number}")

    # Reading it to show it, or to copy it onto an order, is fine. Reading it
    # to match an amount against is the mistake, and the only way to be sure
    # is to have one function own the question.
    allowed_prefixes = (
        "app/domain/fx.py",        # writes it, and renders it on the order
        "app/services/fx_relay.py",  # prints it to the supplier
    )
    unexpected = [
        where for where in offenders
        if not where.startswith(allowed_prefixes)
    ]
    assert not unexpected, (
        "these read supplier_receives directly instead of asking "
        f"settlement.local_leg(): {unexpected}"
    )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

async def test_their_block_matches_both_deals(
    session, acme_support, support_ops, operator
):
    cm = await _awaiting(
        session, acme_support, operator, currency="XAF",
        supplier_sends="3000000", country="CM", subject="a",
    )
    sn = await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN", subject="b",
    )

    parsed = settlement_text.parse(THEIR_BLOCK)
    matches = await settlement.match_lines(session, parsed.lines)

    assert [m.matched for m in matches] == [True, True]
    assert [m.order.id for m in matches] == [cm.id, sn.id]


async def test_a_line_with_no_open_deal_is_reported_not_dropped(
    session, acme_support, support_ops, operator
):
    await _awaiting(
        session, acme_support, operator, currency="XAF",
        supplier_sends="3000000", country="CM",
    )

    parsed = settlement_text.parse(THEIR_BLOCK)
    matches = await settlement.match_lines(session, parsed.lines)

    assert len(matches) == 2, "the unmatched line must still be in the list"
    assert matches[1].matched is False
    assert "no open deal" in matches[1].problem


async def test_two_deals_for_the_same_amount_are_refused(
    session, acme_support, support_ops, operator
):
    """The case worth getting wrong slowly rather than right quickly.

    Guessing here tells one client their money arrived when it did not.
    """
    first = await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN", subject="a",
    )
    second = await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="CI", subject="b",
    )

    parsed = settlement_text.parse("XOF: 20100000/585=34 358,974")
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched is False
    assert "2 open deals" in matches[0].problem
    assert first.display_reference in matches[0].problem
    assert second.display_reference in matches[0].problem


async def test_one_order_cannot_answer_two_lines(
    session, acme_support, support_ops, operator
):
    """Two identical lines in one block must not both land on one deal, or a
    settlement would appear to cover twice what it does."""
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN",
    )

    parsed = settlement_text.parse(
        "XOF: 20100000/585=34 358,974\nXOF: 20100000/585=34 358,974"
    )
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched is True
    assert matches[1].matched is False


async def test_a_deal_not_awaiting_settlement_is_not_a_candidate(
    session, acme_support, support_ops, operator
):
    order = await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN",
    )
    order.status = FxOrderStatus.AWAITING_CLIENT_CONFIRMATION
    await session.flush()

    parsed = settlement_text.parse("XOF: 20100000/585=34 358,974")
    matches = await settlement.match_lines(session, parsed.lines)
    assert matches[0].matched is False


async def test_the_country_comes_from_the_order_when_the_line_gives_a_currency(
    session, acme_support, support_ops, operator
):
    """Their older blocks label by currency. XOF is eight countries, so the
    order is the only thing that knows which one this was."""
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN",
    )

    parsed = settlement_text.parse("XOF: 20100000/585=34 358,974")
    matches = await settlement.match_lines(session, parsed.lines)
    lines = settlement.lines_from(matches)

    assert lines[0].country_code == "SN"


# --------------------------------------------------------------------------
# What the desk is shown before anything is saved
# --------------------------------------------------------------------------

async def test_the_preview_shows_what_was_understood(
    session, acme_support, support_ops, operator
):
    await _awaiting(
        session, acme_support, operator, currency="XAF",
        supplier_sends="3000000", country="CM", subject="a",
    )
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN", subject="b",
    )
    parsed = settlement_text.parse(THEIR_BLOCK)
    matches = await settlement.match_lines(session, parsed.lines)

    preview = handlers.settlement_preview(
        matches, parsed.stated_total, parsed.tx_hash
    )

    assert "nothing saved yet" in preview.lower()
    assert "2 of 2" in preview
    assert "51c86654" in preview


async def test_the_preview_calls_out_a_short_payment(
    session, acme_support, support_ops, operator
):
    """Their 7 September block, against deals that total 163,379.07.

    The desk sees the number before it is recorded, which is the only moment
    at which finding it is cheap.
    """
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="50250000", country="CI", subject="a",
    )
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="45000000", country="CI", subject="b",
    )
    parsed = settlement_text.parse(
        "XOF: 50250000/583=86 192,11\n"
        "XOF: 45000000/583=77 186,964\n"
        "86192 + 77186 = 163 378 USDT"
    )
    matches = await settlement.match_lines(session, parsed.lines)

    preview = handlers.settlement_preview(
        matches, parsed.stated_total, parsed.tx_hash
    )

    assert "short" in preview.lower()
    assert "1.07" in preview


async def test_a_settlement_that_ties_out_says_nothing_about_it(
    session, acme_support, support_ops, operator
):
    """A checker that always finds something is a checker nobody reads."""
    await _awaiting(
        session, acme_support, operator, currency="XAF",
        supplier_sends="3000000", country="CM", subject="a",
    )
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN", subject="b",
    )
    parsed = settlement_text.parse(THEIR_BLOCK)
    matches = await settlement.match_lines(session, parsed.lines)

    preview = handlers.settlement_preview(
        matches, parsed.stated_total, parsed.tx_hash
    )

    assert "short of what" not in preview.lower()
    assert "more than what" not in preview.lower()
    assert "🔴" not in preview


async def test_a_payment_that_is_too_large_reads_as_english(
    session, acme_support, support_ops, operator
):
    """"over of what these deals come to" is what a word slotted into a fixed
    sentence produced, every time a payment was too big. Seen live on
    5 October."""
    await _awaiting(
        session, acme_support, operator, currency="XOF",
        supplier_sends="20100000", country="SN",
    )
    parsed = settlement_text.parse("XOF: 20100000/585\n= 50 000,000 USDT")
    matches = await settlement.match_lines(session, parsed.lines)

    preview = handlers.settlement_preview(
        matches, parsed.stated_total, parsed.tx_hash
    )

    assert "over of" not in preview.lower()
    assert "more than what these deals come to" in preview.lower()


async def test_the_preview_names_an_unmatched_line_and_why(
    session, acme_support, support_ops, operator
):
    await _awaiting(
        session, acme_support, operator, currency="XAF",
        supplier_sends="3000000", country="CM",
    )
    parsed = settlement_text.parse(THEIR_BLOCK)
    matches = await settlement.match_lines(session, parsed.lines)

    preview = handlers.settlement_preview(
        matches, parsed.stated_total, parsed.tx_hash
    )

    assert "Line 2" in preview
    assert "1 of 2" in preview


def test_the_block_with_no_hash_says_so():
    """Recording a settlement before the hash arrives is legitimate. Saying
    nothing about it would leave the desk to notice the absence."""
    preview = handlers.settlement_preview([], None, None)
    assert "no transaction hash" in preview.lower()


# --------------------------------------------------------------------------
# The command itself
# --------------------------------------------------------------------------

def test_the_save_claims_before_it_writes() -> None:
    """Same rule as every other door. Covered by test_every_outward_door for
    the ones that write to a counterparty; this one writes to the ledger, and
    a second tap would record the payment twice."""
    import pathlib

    source = pathlib.Path("app/bot/handlers/fx.py").read_text(encoding="utf-8")
    body = source[source.index("async def settle_save"):]
    body = body[: body.index("# ---")]

    assert body.index("state.clear()") < body.index("settlement.record(")
    assert body.index("_clear_buttons(query)") < body.index("settlement.record(")
