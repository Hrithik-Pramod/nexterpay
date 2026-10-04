"""Our accounts with a supplier, and whose business runs through each.

NexterPay, through Jason on 4 October: "BBS is a big supplier, so we have
multiple accounts. BBS number is Nexterpay Number." Every settlement line they
send ends with one.

The whole value, and the whole limit, is in one sentence of his: the accounts
are **shared**. BBS 1 carries both LuckyStar and Spayz Category B. So the
number narrows a payment down and never identifies it, and everything here is
built on the weaker reading rather than the convenient one.

It still matters. Settlement lines carry no reference, so the only thing to
match on was the amount, and two clients sending fifty million XOF on the same
day is ordinary on a desk doing volume.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import Client
from app.domain import accounts, fx, settlement, settlement_text
from app.domain import work_items as wi
from app.domain.enums import FxOrderStatus
from app.domain.work_items import Actor

# --------------------------------------------------------------------------
# Reading an account off a line
# --------------------------------------------------------------------------

def test_the_number_is_read_however_it_is_written():
    """`Nexterpay 5` on their settlement lines, `BBS 5` in their spreadsheet,
    and a bare `5` from the parser are all the same account."""
    assert accounts.normalise_number("Nexterpay 5") == "5"
    assert accounts.normalise_number("BBS 5") == "5"
    assert accounts.normalise_number("5") == "5"
    assert accounts.normalise_number(" bbs-7 ") == "7"


def test_the_typo_in_their_list_is_not_a_different_supplier():
    """Row 5 reads BBD 13. Jason confirmed on 4 October it is BBS 13."""
    assert accounts.normalise_number("BBD 13") == "13"


# --------------------------------------------------------------------------
# What an account resolves to
# --------------------------------------------------------------------------

async def test_an_account_resolves_to_its_client(session):
    client = Client(name="Pexipay (999win)", code="PEXI")
    session.add(client)
    await session.flush()
    await accounts.record(
        session, supplier_code="BBS", number="5",
        client_name="Pexipay (999win)",
    )

    found = await accounts.clients_on_account(
        session, supplier_code="BBS", number="Nexterpay 5"
    )
    assert [c.name for c in found] == ["Pexipay (999win)"]


async def test_a_shared_account_resolves_to_several(session):
    """BBS 1 is LuckyStar and Spayz Category B both. This is the fact the
    whole module is built around."""
    for name, code in (("LuckyStar", "LUCK"), ("Spayz Category B", "SPAY")):
        session.add(Client(name=name, code=code))
    await session.flush()
    for name in ("LuckyStar", "Spayz Category B"):
        await accounts.record(
            session, supplier_code="BBS", number="1", client_name=name
        )

    found = await accounts.clients_on_account(
        session, supplier_code="BBS", number="1"
    )
    assert len(found) == 2


async def test_numbering_is_per_supplier(session):
    """BBS 5 and SPEX 5 are unrelated, so nothing takes a number without the
    supplier beside it."""
    session.add(Client(name="Pexipay (999win)", code="PEXI"))
    await session.flush()
    await accounts.record(
        session, supplier_code="BBS", number="5", client_name="Pexipay (999win)"
    )

    assert await accounts.clients_on_account(
        session, supplier_code="SPEX", number="5"
    ) == []


async def test_a_client_we_do_not_know_is_kept_but_resolves_to_nothing(session):
    """The list is NexterPay's and names clients not registered here. Dropping
    those rows would quietly lose the mapping the day that client is added."""
    await accounts.record(
        session, supplier_code="BBS", number="9", client_name="MoneyMania"
    )

    assert await accounts.is_known(session, supplier_code="BBS", number="9")
    assert await accounts.clients_on_account(
        session, supplier_code="BBS", number="9"
    ) == []


async def test_registering_the_client_later_makes_it_resolve(session):
    """A reload re-links, so the list does not have to be sent again."""
    await accounts.record(
        session, supplier_code="BBS", number="9", client_name="MoneyMania"
    )
    session.add(Client(name="MoneyMania", code="MONE"))
    await session.flush()

    await accounts.record(
        session, supplier_code="BBS", number="9", client_name="MoneyMania"
    )

    found = await accounts.clients_on_account(
        session, supplier_code="BBS", number="9"
    )
    assert [c.name for c in found] == ["MoneyMania"]


async def test_loading_the_list_twice_does_not_double_it(session):
    for _ in range(3):
        await accounts.record(
            session, supplier_code="BBS", number="3", client_name="TopX"
        )

    from sqlalchemy import select

    from app.db.models import SupplierAccount
    rows = (await session.execute(select(SupplierAccount))).scalars().all()
    assert len(rows) == 1


# --------------------------------------------------------------------------
# What it is actually for
# --------------------------------------------------------------------------

async def _awaiting(session, chat, operator, *, client_name, amount, subject):
    item = await wi.create_work_item(
        session, source_chat=chat, subject=subject,
        original_message="Please provide a rate.", raised_by_name="Gavs D",
    )
    client = await session.get(Client, item.client_id)
    client.name = client_name
    if client.code is None:
        client.code = client_name[:4].upper()
    await session.flush()

    order = await fx.open_order(
        session, client=client, client_work_item=item, actor=Actor.of(operator)
    )
    order.currency_code = "XOF"
    order.supplier_code = "BBS"
    order.supplier_receives = Decimal(amount)
    order.status = FxOrderStatus.AWAITING_SETTLEMENT
    await session.flush()
    return order


async def test_the_account_number_breaks_a_tie_on_amount(
    session, acme_support, support_ops, operator
):
    """The case this exists for.

    Two deals, same currency, same amount - which the platform refuses to
    choose between, and is right to. The account on the line says which client
    it is, so there is no longer a choice to make.
    """
    first = await _awaiting(
        session, acme_support, operator,
        client_name="LuckyStar", amount="20100000", subject="a",
    )
    await _awaiting(
        session, acme_support, operator,
        client_name="TopX", amount="20100000", subject="b",
    )
    await accounts.record(
        session, supplier_code="BBS", number="1", client_name="LuckyStar"
    )

    parsed = settlement_text.parse(
        "XOF: 20100000/585=34 358,974 ( 07/09/2026) Nexterpay 1"
    )
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched
    assert matches[0].order.id == first.id


async def test_a_shared_account_still_refuses_when_both_fit(
    session, acme_support, support_ops, operator
):
    """Narrowing is not deciding. If both candidates run through the same
    account, the number has told us nothing and the refusal stands."""
    await _awaiting(
        session, acme_support, operator,
        client_name="LuckyStar", amount="20100000", subject="a",
    )
    await _awaiting(
        session, acme_support, operator,
        client_name="Spayz Category B", amount="20100000", subject="b",
    )
    for name in ("LuckyStar", "Spayz Category B"):
        await accounts.record(
            session, supplier_code="BBS", number="1", client_name=name
        )

    parsed = settlement_text.parse(
        "XOF: 20100000/585=34 358,974 ( 07/09/2026) Nexterpay 1"
    )
    matches = await settlement.match_lines(session, parsed.lines)

    assert not matches[0].matched
    assert "2 open deals" in matches[0].problem


async def test_an_unknown_account_does_not_hide_a_deal(
    session, acme_support, support_ops, operator
):
    """A half-loaded mapping must not make things worse than no mapping.

    If the account resolves to nothing - not loaded yet, or a client not
    registered here - the line still matches on amount exactly as it did
    before. The mapping is there to help.
    """
    order = await _awaiting(
        session, acme_support, operator,
        client_name="LuckyStar", amount="20100000", subject="a",
    )

    parsed = settlement_text.parse(
        "XOF: 20100000/585=34 358,974 ( 07/09/2026) Nexterpay 99"
    )
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched
    assert matches[0].order.id == order.id


async def test_a_line_with_no_account_behaves_as_before(
    session, acme_support, support_ops, operator
):
    await _awaiting(
        session, acme_support, operator,
        client_name="LuckyStar", amount="20100000", subject="a",
    )

    parsed = settlement_text.parse("XOF: 20100000/585=34 358,974")
    matches = await settlement.match_lines(session, parsed.lines)

    assert matches[0].matched
