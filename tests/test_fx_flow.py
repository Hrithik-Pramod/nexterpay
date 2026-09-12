"""The FX deal, walked end to end.

NexterPay's route of 12 September, in order:

    1. client asks for a rate            free format, their group
    2. we ask a supplier                 free format, their group
    3. supplier quotes us                no good -> back to 2
    4. we give the client the rate       they confirm, then say what they want
    5. we create the client's order      amount, rate, what they receive, name
    6. client confirms
    7. we create the supplier's order
    8. supplier accepts
    9. chasing                           can be days
   10. supplier confirms the hash
   11. we pass it to the client
   12. client confirms receipt           closed

Chasing is not a state and does not appear here, which is the point: it is
what you do inside Awaiting settlement, recorded as messages. A status for it
would mean two states that both say "waiting on the supplier" and neither says
anything the other does not.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Client
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor


async def _deal(session, acme_support, operator):
    """A client request, and a deal opened against it."""
    item = await wi.create_work_item(
        session,
        source_chat=acme_support,
        subject="EUR to USDT",
        original_message="What rate can you do for 250k EUR?",
        raised_by_name="Tom Baker",
    )
    client = await session.get(Client, item.client_id)
    # The fixture registers the group without a code, the way a real group is
    # registered before anyone runs /npsetcode. References only read ACME- once
    # one exists, so the tests set it the way an administrator would.
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    return item, client, order


async def _supplier(session) -> Client:
    """A supplier is an ordinary counterparty. `is_supplier` lives on the chat,
    not here - the same organisation can be a client on one ticket and the
    supplier on another."""
    supplier = Client(name="Supplier Pexi", code="SPEX")
    session.add(supplier)
    await session.flush()
    return supplier


async def _to_settlement(session, acme_support, operator):
    """A deal walked as far as the supplier accepting, where chasing begins."""
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.14"), actor=actor,
    )
    await fx.quote_client(session, order, rate=Decimal("1.16"), actor=actor)
    await fx.create_client_order(
        session, order, account_name="Acme", rate=Decimal("1.16"),
        pays=Decimal("1000"), pays_currency="EUR",
        receives=Decimal("1160"), receives_currency="USDT", actor=actor,
    )
    await fx.client_confirms(session, order, actor=Actor.system())
    await fx.create_supplier_order(
        session, order, account_name="Nexterpay7", rate=Decimal("1.14"),
        pays=Decimal("1140"), pays_currency="USDT",
        receives=Decimal("1000"), receives_currency="EUR", actor=actor,
    )
    await fx.supplier_accepts(session, order, actor=Actor.system())
    return order


# --------------------------------------------------------------------------
# The whole route
# --------------------------------------------------------------------------

async def test_a_deal_runs_from_enquiry_to_closed(
    session, acme_support, support_ops, operator
):
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)

    assert order.status is FxOrderStatus.RATE_REQUESTED
    assert order.status.waiting_on == "Supplier"

    # 3. the supplier quotes us
    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.1408"), actor=actor,
    )
    assert order.supplier_rate == Decimal("1.1408")

    # 4. we add our margin and quote the client
    await fx.quote_client(session, order, rate=Decimal("1.1642"), actor=actor)
    assert order.status is FxOrderStatus.RATE_QUOTED
    assert order.status.waiting_on == "Client"

    # 5. the client's order
    await fx.create_client_order(
        session, order,
        account_name="Acme Payments Ltd",
        rate=Decimal("1.1642"),
        pays=Decimal("250000"), pays_currency="eur",
        receives=Decimal("291050"), receives_currency="usdt",
        actor=actor,
    )
    assert order.status is FxOrderStatus.AWAITING_CLIENT_CONFIRMATION
    assert order.client_pays_currency == "EUR"   # upper-cased on the way in

    # 6. they confirm
    await fx.client_confirms(session, order, actor=Actor.system())
    assert order.status is FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE
    assert order.client_confirmed_at is not None

    # 7. the supplier's order, in our name rather than the client's
    await fx.create_supplier_order(
        session, order,
        account_name="Nexterpay7",
        rate=Decimal("1.1408"),
        pays=Decimal("285200"), pays_currency="usdt",
        receives=Decimal("250000"), receives_currency="eur",
        actor=actor,
    )

    # 8. they accept
    await fx.supplier_accepts(session, order, actor=Actor.system())
    assert order.status is FxOrderStatus.AWAITING_SETTLEMENT

    # 9 and 10. chasing happens in here, then the hash
    await fx.record_hash(
        session, order, tx_hash="a3f19c8b2e7d4016f5c9ab3210de4477", actor=actor
    )
    assert order.status is FxOrderStatus.AWAITING_RECEIPT
    assert order.settled_at is not None

    # 12. the client has the funds, and that is what closes it
    await fx.client_confirms_receipt(session, order, actor=Actor.system())
    assert order.status is FxOrderStatus.CLOSED
    assert order.closed_at is not None
    assert not order.is_open


async def test_the_hash_is_not_the_end(session, acme_support, support_ops, operator):
    """NexterPay, 12 September: "the supplier confirms the hash. and we confirm
    to client, once client confirm receipt, ticket is closed". The money moving
    is not the same as the client having it."""
    order = await _to_settlement(session, acme_support, operator)
    await fx.record_hash(session, order, tx_hash="b" * 32, actor=Actor.of(operator))

    assert order.status is not FxOrderStatus.CLOSED
    assert order.status.waiting_on == "Client"


@pytest.mark.parametrize("bad", ["", "   ", "abc123", "a3f19c8b 2e7d4016f5c9ab32"])
async def test_a_hash_that_is_not_a_hash_is_refused(
    session, acme_support, support_ops, operator, bad
):
    """It goes to the client as proof of settlement, so a half-pasted one is
    worth catching before it does."""
    order = await _to_settlement(session, acme_support, operator)

    with pytest.raises(fx.FxError):
        await fx.record_hash(session, order, tx_hash=bad, actor=Actor.of(operator))

    assert order.status is FxOrderStatus.AWAITING_SETTLEMENT
    assert order.tx_hash is None


# --------------------------------------------------------------------------
# The two return paths
# --------------------------------------------------------------------------

async def test_we_can_tell_a_supplier_their_rate_is_no_good(
    session, acme_support, support_ops, operator
):
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.09"), actor=actor,
    )
    await fx.reject_supplier_rate(
        session, order, reason="too far off the market", actor=actor
    )

    # Still waiting on a price, and the rejected one is gone rather than
    # lingering where it could be quoted by mistake.
    assert order.status is FxOrderStatus.RATE_REQUESTED
    assert order.supplier_rate is None

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.1408"), actor=actor,
    )
    assert order.supplier_rate == Decimal("1.1408")


async def test_a_client_rejecting_returns_to_the_same_deal(
    session, acme_support, support_ops, operator
):
    """Not a new order. A flow with no way back gets worked around, and the
    workaround loses the link between the two quotes."""
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)
    reference = order.reference

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.14"), actor=actor,
    )
    await fx.quote_client(session, order, rate=Decimal("1.20"), actor=actor)
    await fx.reject_client_rate(session, order, reason="too high", actor=actor)
    assert order.status is FxOrderStatus.RATE_REJECTED

    await fx.quote_client(session, order, rate=Decimal("1.17"), actor=actor)
    assert order.status is FxOrderStatus.RATE_QUOTED
    assert order.reference == reference
    assert order.client_rate == Decimal("1.17")


# --------------------------------------------------------------------------
# Sequence enforcement
# --------------------------------------------------------------------------

async def test_a_hash_cannot_be_recorded_before_a_rate_is_agreed(
    session, acme_support, support_ops, operator
):
    """The cost NexterPay accepted knowingly on 5 September: somebody who
    settles on a phone call records the steps afterwards rather than jumping to
    the end."""
    _, _, order = await _deal(session, acme_support, operator)

    with pytest.raises(fx.FxError) as caught:
        await fx.record_hash(session, order, tx_hash="c" * 32, actor=Actor.of(operator))

    assert "awaiting settlement" in str(caught.value).lower()


async def test_quoting_under_the_supplier_rate_is_refused(
    session, acme_support, support_ops, operator
):
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.1408"), actor=actor,
    )
    with pytest.raises(fx.FxError) as caught:
        await fx.quote_client(session, order, rate=Decimal("1.10"), actor=actor)

    assert "lose money" in str(caught.value)


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------

async def test_the_client_reference_omits_the_supplier(
    session, acme_support, support_ops, operator
):
    actor = Actor.of(operator)
    _, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)
    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.14"), actor=actor,
    )

    assert order.display_reference == f"FXACME-SPEX-{order.reference}"
    assert order.client_reference == f"FXACME-{order.reference}"
    assert "SPEX" not in order.client_reference


async def test_fx_references_number_separately_from_requests(
    session, acme_support, support_ops, operator
):
    """So FX orders read 1000, 1001, 1002 rather than taking every third number
    from a shared pool and looking as though something has gone missing."""
    _, _, first = await _deal(session, acme_support, operator)
    _, _, second = await _deal(session, acme_support, operator)

    assert second.reference == first.reference + 1


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------

async def test_every_figure_is_written_to_the_history(
    session, acme_support, support_ops, operator
):
    """"The rate was set to 1.1642 by Gavin" is the line somebody reads back six
    weeks later when a client disputes what was agreed."""
    from app.domain.history import load_events

    actor = Actor.of(operator)
    item, _, order = await _deal(session, acme_support, operator)
    supplier = await _supplier(session)

    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=None,
        rate=Decimal("1.1408"), actor=actor,
    )
    await fx.quote_client(session, order, rate=Decimal("1.1642"), actor=actor)

    events = await load_events(session, item)
    payloads = [e.payload for e in events if e.payload]

    assert any(p.get("rate") == "1.1642" for p in payloads), payloads
    assert any(p.get("rate") == "1.1408" for p in payloads), payloads
    # Stored as strings, because a float would quietly round somebody's money
    # in the one place it must not be rounded.
    for payload in payloads:
        assert not isinstance(payload.get("rate"), float)


async def test_the_history_lives_on_the_client_request(
    session, acme_support, support_ops, operator
):
    """So /nphistory shows the whole deal rather than half of it."""
    item, _, _ = await _deal(session, acme_support, operator)

    from app.domain.history import load_events

    events = await load_events(session, item)
    assert events, "the deal recorded nothing against the request it belongs to"
    assert all(e.work_item_id == item.id for e in events)


# --------------------------------------------------------------------------
# Parsing what somebody typed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "typed,expected",
    [
        ("250000", Decimal("250000")),
        ("250,000", Decimal("250000")),
        ("1,250,000.50", Decimal("1250000.50")),
        (" 1.1642 ", Decimal("1.1642")),
    ],
)
def test_figures_people_type_are_understood(typed, expected) -> None:
    assert fx.parse_amount(typed) == expected


@pytest.mark.parametrize("typed", ["", "  ", "about 1.2m", "1.2m", "£250000", "-5", "0"])
def test_figures_that_are_not_figures_are_refused(typed) -> None:
    """This number is going to a counterparty as a commitment. "about 1.2m"
    must not quietly become 1.2."""
    with pytest.raises(fx.FxError):
        fx.parse_amount(typed)


def test_money_is_never_a_float() -> None:
    """0.1 + 0.2 is not 0.3 in binary floating point, and this is somebody's
    money."""
    assert isinstance(fx.parse_amount("1.1642"), Decimal)


@pytest.mark.parametrize(
    "value,shown",
    [
        (Decimal("1.1642000"), "1.1642"),
        (Decimal("250000"), "250,000"),
        (Decimal("1250000.50"), "1,250,000.50"),
        (Decimal("1000"), "1,000"),
    ],
)
def test_figures_are_shown_the_way_finance_writes_them(value, shown) -> None:
    assert fx.format_money(value) == shown


def test_the_explorer_link_is_built_for_tron() -> None:
    link = fx.explorer_link("tron", "a3f19c8b2e7d4016f5c9ab3210de4477")
    assert link and link.endswith("a3f19c8b2e7d4016f5c9ab3210de4477")
    assert "tronscan" in link


def test_an_unknown_chain_gets_no_link_rather_than_a_wrong_one() -> None:
    """Ethereum is phase two. Until then a guessed link is worse than none."""
    assert fx.explorer_link("ethereum", "0xabc") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("FXACME-1042", 1042),
        ("FXACME-SPEX-1042", 1042),
        ("FX#1042", 1042),
        ("1042", 1042),
        ("nothing here", None),
    ],
)
def test_references_are_recognised_however_they_are_written(text, expected) -> None:
    assert fx.parse_fx_reference(text) == expected
