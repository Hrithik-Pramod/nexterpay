"""The loop that tells the desk when money arrives.

Everything that decides anything is in `wallet.py` and tested there without a
network. This covers the joining-up: what gets announced, what deliberately
does not, and where it is said.

The thing worth guarding hardest is that it **proposes and never records**. A
payment matching one open deal exactly is still only a proposal, because what
it would be asserting is that somebody's money has arrived - and the platform
has one fact, an amount, where a person has several.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot.registry import get_setting, register_operations_chat, set_setting
from app.db.models import Client, Settlement
from app.domain import fx
from app.domain import work_items as wi
from app.domain.enums import Department, FxOrderStatus
from app.domain.work_items import Actor
from app.services import wallet, wallet_watch
from app.services.gateway import FakeGateway

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
ADDRESS = "TPFUAVKJYkLDu6xtzzSGXcXYR5bG1VBBjq"
HASH_A = "a" * 64
HASH_B = "b" * 64


class FakeChain:
    """A chain that returns what the test says it does."""

    def __init__(self, payments=None):
        self.payments = payments or []
        self.asked = []

    async def incoming_usdt(self, address, *, since=None):
        self.asked.append((address, since))
        return [p for p in self.payments if since is None or p.at > since]


def _payment(amount, *, at=NOW, tx=HASH_A):
    return wallet.IncomingPayment(
        tx_hash=tx, amount_usdt=Decimal(amount), at=at
    )


@pytest.fixture
async def finance_ops(session):
    return await register_operations_chat(
        session,
        telegram_chat_id=-1001000000009,
        department=Department.FINANCE,
        title="Finance Operations",
    )


async def _awaiting(session, acme_support, operator, *, local, rate, subject="deal"):
    item = await wi.create_work_item(
        session, source_chat=acme_support, subject=subject,
        original_message="Please provide a rate.", raised_by_name="Gavs D",
    )
    client = await session.get(Client, item.client_id)
    if client.code is None:
        client.code = "ACME"
        await session.flush()
    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    order.supplier_receives = Decimal(local)
    order.supplier_rate = Decimal(rate)
    order.currency_code = "XOF"
    order.status = FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()
    return order


# --------------------------------------------------------------------------
# When it says nothing
# --------------------------------------------------------------------------

async def test_no_wallet_set_means_no_work(session, finance_ops):
    """Not an error. This runs on a timer, and a loop that logs an exception
    every five minutes for a setting nobody has filled in is a loop whose
    logs stop being read."""
    gw = FakeGateway()
    chain = FakeChain([_payment("100")])

    assert await wallet_watch.poll(session, gw, chain) == 0
    assert chain.asked == [], "it should not even ask"


async def test_nothing_arriving_says_nothing(session, finance_ops):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    gw = FakeGateway()

    assert await wallet_watch.poll(session, gw, FakeChain([])) == 0
    assert not gw.calls


async def test_a_payment_the_desk_already_settled_is_not_announced(
    session, acme_support, support_ops, finance_ops, operator
):
    """They will often beat the poll - the supplier posts the hash in the
    group and somebody pastes it straight in. Announcing it afterwards would
    be the platform telling them about something they did."""
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    session.add(Settlement(reference=1, tx_hash=HASH_A, recorded_by_name="peter"))
    await session.flush()

    gw = FakeGateway()
    announced = await wallet_watch.poll(
        session, gw, FakeChain([_payment("100", tx=HASH_A)])
    )

    assert announced == 0
    assert not gw.calls


async def test_without_a_finance_group_it_says_nothing_anywhere(
    session, acme_support, support_ops, operator
):
    """Rather than falling back to whichever operations group came first,
    which would put settlement figures in front of a desk that has no
    business with them."""
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    gw = FakeGateway()

    assert await wallet_watch.poll(session, gw, FakeChain([_payment("100")])) == 0
    assert not gw.calls


# --------------------------------------------------------------------------
# When it speaks
# --------------------------------------------------------------------------

async def test_a_matching_payment_is_announced_to_finance(
    session, acme_support, support_ops, finance_ops, operator
):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    order = await _awaiting(
        session, acme_support, operator, local="20100000", rate="585"
    )
    gw = FakeGateway()

    announced = await wallet_watch.poll(
        session, gw, FakeChain([_payment("34358.974359")])
    )

    assert announced == 1
    said = gw.all_text_to(finance_ops.telegram_chat_id)
    assert order.display_reference in said


async def test_it_proposes_and_never_records(
    session, acme_support, support_ops, finance_ops, operator
):
    """The guard that matters.

    A payment matching one deal exactly is still only a proposal. What it
    would be asserting is that somebody's money has arrived, and the platform
    has one fact where a person has several.
    """
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    order = await _awaiting(
        session, acme_support, operator, local="20100000", rate="585"
    )

    await wallet_watch.poll(
        session, FakeGateway(), FakeChain([_payment("34358.974359")])
    )

    assert order.status is FxOrderStatus.AWAITING_SETTLEMENT
    assert order.tx_hash is None


async def test_an_ambiguous_payment_names_both_and_guesses_neither(
    session, acme_support, support_ops, finance_ops, operator
):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    first = await _awaiting(
        session, acme_support, operator, local="20100000", rate="585", subject="a"
    )
    second = await _awaiting(
        session, acme_support, operator, local="20100000", rate="585", subject="b"
    )
    gw = FakeGateway()

    await wallet_watch.poll(
        session, gw, FakeChain([_payment("34358.974359")])
    )

    said = gw.all_text_to(finance_ops.telegram_chat_id)
    assert first.display_reference in said
    assert second.display_reference in said
    assert "not guessed" in said.lower()


async def test_a_payment_nobody_expected_is_still_announced(
    session, acme_support, support_ops, finance_ops, operator
):
    """Either a supplier paying early, or money nobody has accounted for.
    Both are things the desk needs to know."""
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    gw = FakeGateway()

    assert await wallet_watch.poll(
        session, gw, FakeChain([_payment("777")])
    ) == 1
    assert "no open deal" in gw.all_text_to(
        finance_ops.telegram_chat_id
    ).lower()


async def test_their_rounding_slip_is_announced_as_short(
    session, acme_support, support_ops, finance_ops, operator
):
    """163,378 against 163,379.07. The payment is the one being waited for,
    and the desk is told it is light rather than left to find out."""
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    await _awaiting(
        session, acme_support, operator, local="95250000", rate="583"
    )
    gw = FakeGateway()

    await wallet_watch.poll(session, gw, FakeChain([_payment("163378")]))

    assert "short" in gw.all_text_to(finance_ops.telegram_chat_id).lower()


# --------------------------------------------------------------------------
# Not saying it twice
# --------------------------------------------------------------------------

async def test_a_payment_is_only_announced_once(
    session, acme_support, support_ops, finance_ops, operator
):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    chain = FakeChain([_payment("777")])
    gw = FakeGateway()

    assert await wallet_watch.poll(session, gw, chain) == 1
    assert await wallet_watch.poll(session, gw, chain) == 0


async def test_the_high_water_mark_is_kept(
    session, acme_support, support_ops, finance_ops, operator
):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    await wallet_watch.poll(
        session, FakeGateway(), FakeChain([_payment("777", at=NOW)])
    )

    assert await get_setting(session, wallet_watch.SEEN_SETTING) == NOW.isoformat()


async def test_a_later_payment_is_still_announced(
    session, acme_support, support_ops, finance_ops, operator
):
    await set_setting(session, wallet.WALLET_SETTING, ADDRESS)
    gw = FakeGateway()
    later = NOW + timedelta(minutes=30)

    await wallet_watch.poll(session, gw, FakeChain([_payment("777", at=NOW)]))
    announced = await wallet_watch.poll(
        session, gw,
        FakeChain([_payment("777", at=NOW), _payment("888", at=later, tx=HASH_B)]),
    )

    assert announced == 1


# --------------------------------------------------------------------------
# The address itself
# --------------------------------------------------------------------------

def test_a_tron_address_is_checked_rather_than_stored():
    """A wrong address produces silence from the watcher, and silence is
    indistinguishable from nothing having arrived - it would be found days
    later by a client chasing money the platform believed was never sent."""
    assert wallet.parse_address(f"  {ADDRESS} ") == ADDRESS

    for wrong in ("", "not-an-address", ADDRESS[:-1], "X" + ADDRESS[1:]):
        with pytest.raises(wallet.WalletError):
            wallet.parse_address(wrong)


def test_the_confusable_characters_are_refused():
    """Base58 leaves out 0, O, I and l precisely because people confuse them,
    so refusing them is refusing a typo rather than an address."""
    with pytest.raises(wallet.WalletError):
        wallet.parse_address("T" + "0" * 33)
