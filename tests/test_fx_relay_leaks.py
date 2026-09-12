"""What actually arrives in each counterparty's group.

`test_fx_margin_never_crosses` proves the view objects are clean. This proves
the messages are — because a correct view composed into a message by a
function that also has the order in scope is still a leak waiting to happen,
and that is the mistake worth testing for rather than assuming away.

Every test here sends something for real through the fake gateway and then
reads the client's group and the supplier's group as Telegram would.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest
import pytest_asyncio

from app.bot.registry import register_client_chat
from app.db.models import Client
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import Department, FxSide
from app.domain.work_items import Actor
from app.services import fx_relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPLIER_CHAT = -1002000000077
OPS_CHAT = -1001000000001

# The supplier's side of the deal. None of this may appear in the client's
# group, and the client's name may not appear in the supplier's.
SUPPLIER_RATE = Decimal("1.1408")
CLIENT_RATE = Decimal("1.1642")


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def pexi_support(session, support_ops):
    return await register_client_chat(
        session,
        telegram_chat_id=SUPPLIER_CHAT,
        client_name="Supplier Pexi",
        department=Department.SUPPORT,
        title="Pexi — Support",
        is_supplier=True,
    )


async def _deal(session, acme_support, pexi_support, operator, gw):
    """A deal with both halves live and every figure filled in."""
    actor = Actor.of(operator)

    client_item = await wi.create_work_item(
        session, source_chat=acme_support, subject="EUR to USDT",
        original_message="250k EUR please", raised_by_name="Tom Baker",
    )
    await wi.attach_topic(session, client_item, 9001)
    supplier_item = await wi.create_work_item(
        session, source_chat=pexi_support, subject="EUR to USDT",
        original_message="rate for 250k EUR", raised_by_name="Gavin",
    )
    await wi.attach_topic(session, supplier_item, 9002)

    client = await session.get(Client, client_item.client_id)
    if client.code is None:
        client.code = "ACME"
    supplier = await session.get(Client, supplier_item.client_id)
    if supplier.code is None:
        supplier.code = "SPEX"
    await session.flush()

    order = await fx.open_order(
        session, client=client, client_work_item=client_item, actor=actor
    )
    await fx.record_supplier_quote(
        session, order, supplier=supplier, supplier_work_item=supplier_item,
        rate=SUPPLIER_RATE, actor=actor,
    )
    await fx.quote_client(session, order, rate=CLIENT_RATE, actor=actor)
    await fx.create_client_order(
        session, order, account_name="Acme Payments Ltd", rate=CLIENT_RATE,
        pays=Decimal("250000"), pays_currency="EUR",
        receives=Decimal("291050"), receives_currency="USDT", actor=actor,
    )
    await fx.client_confirms(session, order, actor=Actor.system())
    await fx.create_supplier_order(
        session, order, account_name="Nexterpay7", rate=SUPPLIER_RATE,
        pays=Decimal("285200"), pays_currency="USDT",
        receives=Decimal("250000"), receives_currency="EUR", actor=actor,
    )
    return order, actor


# --------------------------------------------------------------------------
# The client's group
# --------------------------------------------------------------------------

async def test_the_client_group_never_sees_the_supplier_rate(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    order, actor = await _deal(session, acme_support, pexi_support, operator, gw)
    await fx_relay.send_order(session, gw, order, FxSide.CLIENT, actor=actor)

    seen = gw.all_text_to(CLIENT_CHAT)
    assert "1.1408" not in seen, f"the supplier's rate reached the client:\n{seen}"
    assert "285,200" not in seen
    assert "285200" not in seen
    assert "Nexterpay7" not in seen
    assert "SPEX" not in seen
    assert "Supplier Pexi" not in seen


async def test_the_client_group_sees_its_own_figures(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    """The other half. A message that leaked nothing because it said nothing
    would pass the test above and be useless."""
    order, actor = await _deal(session, acme_support, pexi_support, operator, gw)
    await fx_relay.send_order(session, gw, order, FxSide.CLIENT, actor=actor)

    seen = gw.all_text_to(CLIENT_CHAT)
    assert "1.1642" in seen
    assert "250,000" in seen
    assert "291,050" in seen
    assert "FXACME-" in seen


# --------------------------------------------------------------------------
# The supplier's group
# --------------------------------------------------------------------------

async def test_the_supplier_group_never_sees_the_client(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    """They are quoting into an account, not to a named customer."""
    order, actor = await _deal(session, acme_support, pexi_support, operator, gw)
    await fx_relay.send_order(session, gw, order, FxSide.SUPPLIER, actor=actor)

    seen = gw.all_text_to(SUPPLIER_CHAT)
    assert "Acme" not in seen, f"the client was named to the supplier:\n{seen}"
    assert "1.1642" not in seen, "our rate reached the supplier"
    assert "291,050" not in seen
    assert "Nexterpay7" in seen


async def test_a_rejected_supplier_is_told_nothing_about_the_price(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    """A supplier who learns what beat them learns the market we buy in."""
    order, actor = await _deal(session, acme_support, pexi_support, operator, gw)
    await fx_relay.notify_rejected(
        session, gw, order, actor=actor, reason="too far off the market"
    )

    seen = gw.all_text_to(SUPPLIER_CHAT)
    assert "1.1408" not in seen
    assert "1.1642" not in seen
    assert "too far off the market" not in seen, "our internal reason went outward"
    assert "not able to work with that rate" in seen


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------

async def test_the_settlement_carries_the_hash_and_a_link(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    order, actor = await _deal(session, acme_support, pexi_support, operator, gw)
    await fx.supplier_accepts(session, order, actor=Actor.system())
    await fx.record_hash(session, order, tx_hash="a" * 32, actor=actor)

    await fx_relay.send_settlement(session, gw, order, actor=actor)

    seen = gw.all_text_to(CLIENT_CHAT)
    assert "a" * 32 in seen
    assert "tronscan" in seen
    assert "1.1408" not in seen, "the supplier's rate rode along with the hash"


# --------------------------------------------------------------------------
# The desk sees everything, and only the desk
# --------------------------------------------------------------------------

async def test_the_desk_summary_shows_both_sides_and_the_margin(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    """The one place the two rates sit together. Staff-only by construction:
    it is never passed to anything that writes to a counterparty."""
    order, _ = await _deal(session, acme_support, pexi_support, operator, gw)
    summary = fx_relay.desk_summary(order)

    assert "1.1642" in summary
    assert "1.1408" in summary
    assert "0.0234" in summary  # the margin


async def test_the_desk_summary_is_never_sent_outward(
    session, acme_support, pexi_support, support_ops, operator, gw
):
    """Checked structurally, because the failure mode is somebody reaching for
    the fuller summary when composing a message to a client."""
    source = inspect.getsource(fx_relay)
    tree = ast.parse(source)

    outward = {"send_order", "send_settlement", "notify_rejected"}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in outward:
            body = ast.unparse(node)
            assert "desk_summary" not in body, (
                f"{node.name} composes the desk summary, which carries both rates"
            )


def test_only_these_functions_may_write_to_a_counterparty() -> None:
    """The list is deliberately short and deliberately checked.

    A fourth way outward is how a leak arrives - not by somebody rewriting
    `view_for`, but by adding a helpful new notification that reads the order
    directly. If this test fails, the new function needs the same scrutiny the
    other three had, not an addition to the list.
    """
    tree = ast.parse(inspect.getsource(fx_relay))
    writers = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        body = ast.unparse(node)
        if "counterparty.telegram_chat_id" in body:
            writers.add(node.name)

    assert writers == {"send_order", "send_settlement", "notify_rejected"}, (
        f"the ways out of NexterPay have changed: {sorted(writers)}"
    )


async def test_an_order_cannot_be_sent_to_a_side_that_has_no_request(
    session, acme_support, support_ops, operator, gw
):
    """Refused out loud rather than sent to whichever group happens to be to
    hand. There is no default counterparty."""
    client_item = await wi.create_work_item(
        session, source_chat=acme_support, subject="EUR to USDT",
        original_message="250k please", raised_by_name="Tom Baker",
    )
    client = await session.get(Client, client_item.client_id)
    order = await fx.open_order(
        session, client=client, client_work_item=client_item, actor=Actor.of(operator)
    )

    with pytest.raises(fx.FxError) as caught:
        await fx_relay.send_order(
            session, gw, order, FxSide.SUPPLIER, actor=Actor.of(operator)
        )

    assert "no supplier request" in str(caught.value)
    assert gw.all_text_to(SUPPLIER_CHAT) == ""
