"""The reference format, and the rule that the supplier code stays internal.

NexterPay file tickets as Client / Supplier / Ticket. Because Telegram topics
are flat, that structure lives in the reference and the topic title. The one
rule that matters beyond formatting: a client must never see which supplier
their issue was filed against.
"""

from __future__ import annotations

import pytest

from app.db.models import Client, WorkItem
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


def test_reference_shapes() -> None:
    item = WorkItem(reference=1042)
    assert item.display_reference == "#1042"
    assert item.client_reference == "#1042"

    item.client_code = "ACME"
    assert item.display_reference == "ACME-1042"
    assert item.client_reference == "ACME-1042"

    item.supplier_code = "SPEX"
    assert item.display_reference == "ACME-SPEX-1042"
    assert item.client_reference == "ACME-1042", "supplier code must not reach the client"


async def test_tickets_raised_before_codes_keep_their_old_reference(
    session, acme_support, support_ops, gw
):
    """Existing tickets are not renumbered.

    A reference already quoted in an email should not change underneath the
    person holding it, so a counterparty with no code produces #1000 as before.
    """
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    assert item.display_reference.startswith("#")


async def test_the_supplier_code_never_reaches_the_client(
    session, acme_support, support_ops, operator, gw
):
    """The leak test for the filing structure.

    Everything outbound uses client_reference. If someone switches one of
    those to display_reference, this fails.
    """
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    item.supplier_code = "SPEX"
    await session.flush()

    await relay.send_client_reply(session, gw, item, Actor.of(operator), "looking into it.")

    outbound = gw.all_text_to(CLIENT_CHAT)
    assert "ACME-" in outbound
    assert "SPEX" not in outbound, "the supplier code leaked into the client group"

    # Internally the full reference is what identifies it. The topic title and
    # header are rewritten when a supplier is filed; that is covered with the
    # File under button rather than here.
    assert item.display_reference == f"ACME-SPEX-{item.reference}"


async def test_the_code_is_copied_onto_the_ticket_at_creation(
    session, acme_support, support_ops, gw
):
    """Not read through the relationship - that would be lazy IO in async code."""
    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    assert item.client_code == "ACME"
    assert item.display_reference == f"ACME-{item.reference}"


# --------------------------------------------------------------------------
# Filing a ticket under a supplier
# --------------------------------------------------------------------------


async def _coded(session, chat, code):
    client = await session.get(Client, chat.client_id)
    client.code = code
    await session.flush()
    return client


async def _supplier(session, name="Supplier Pexi", code="SPEX"):
    supplier = Client(name=name, code=code)
    session.add(supplier)
    await session.flush()
    return supplier


async def test_filing_renames_the_topic_to_match_the_new_reference(
    session, acme_support, support_ops, operator, gw
):
    """The reference changes when a ticket is filed, so the title must follow.

    Otherwise the ticket answers to one name in conversation and a different
    one in the sidebar, which defeats the point of filing it.
    """
    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    supplier = await _supplier(session)

    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    assert item.display_reference == f"ACME-SPEX-{item.reference}"
    renamed = gw.topic_names[(OPS_CHAT, item.topic_id)]
    # The traffic light leads the title now, so the reference comes second.
    # The topic list truncates from the right, which is why the light is first.
    assert renamed.startswith(relay.LIGHT_UNCLAIMED)
    assert f"ACME-SPEX-{item.reference}" in renamed
    assert "Settlement" in renamed


async def test_filing_is_recorded_in_the_history(
    session, acme_support, support_ops, operator, gw
):
    from app.domain.history import load_events, render_history

    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    supplier = await _supplier(session)
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    history = "\n".join(render_history(await load_events(session, item)))
    assert "Filed under Supplier Pexi" in history
    assert f"ACME-{item.reference}" in history
    assert f"ACME-SPEX-{item.reference}" in history


async def test_filing_tells_the_client_nothing(
    session, acme_support, support_ops, operator, gw
):
    """Which supplier a request was filed against is not the client's business."""
    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    before = len(gw.messages_to(CLIENT_CHAT))
    supplier = await _supplier(session)

    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)


async def test_a_supplier_without_a_code_cannot_be_filed_against(
    session, acme_support, support_ops, operator, gw
):
    from app.domain.errors import DomainError

    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    uncoded = Client(name="Nameless Supplier")
    session.add(uncoded)
    await session.flush()

    with pytest.raises(DomainError):
        await relay.file_under(session, gw, item, uncoded, Actor.of(operator))


async def test_filing_the_same_supplier_twice_does_nothing(
    session, acme_support, support_ops, operator, gw
):
    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    supplier = await _supplier(session)
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))
    renames = len([c for c in gw.calls if c.method == "rename_topic"])

    await relay.file_under(session, gw, item, supplier, Actor.of(operator))
    assert len([c for c in gw.calls if c.method == "rename_topic"]) == renames


async def test_a_counterparty_can_exist_without_a_telegram_group(
    session, acme_support, support_ops, operator, gw
):
    """Suppliers can be filed against without ever having a group.

    Most of NexterPay's suppliers will never have a Telegram group with them.
    If filing required one, the filing structure would be unusable for exactly
    the cases it was asked for.
    """
    await _coded(session, acme_support, "ACME")
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )

    # No chat, no registration - just the counterparty. (Deliberately not
    # asserting on .chats: touching a relationship here lazy-loads, which is
    # the MissingGreenlet trap this codebase has hit more than once.)
    groupless = Client(name="Supplier With No Group", code="SPNG")
    session.add(groupless)
    await session.flush()

    await relay.file_under(session, gw, item, groupless, Actor.of(operator))

    assert item.display_reference == f"ACME-SPNG-{item.reference}"
    assert "SPNG" not in gw.all_text_to(CLIENT_CHAT)


async def test_a_second_group_for_the_same_client_shares_its_code(
    session, acme_support, support_ops
):
    """One counterparty, one code, however many groups they have.

    NexterPay have a Support group and a Compliance group with the same
    client. Both must resolve to one record, or the client appears twice in
    every list and searching their code returns half their work.
    """
    from app.bot.registry import register_client_chat, register_operations_chat
    from app.db.models import Client
    from app.domain.enums import Department

    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    await register_operations_chat(
        session, telegram_chat_id=-1001000000009,
        department=Department.COMPLIANCE, title="Compliance Operations",
    )
    second = await register_client_chat(
        session, telegram_chat_id=-1002000000009,
        client_name="Acme Payments",          # the same name, deliberately
        department=Department.COMPLIANCE, title="Acme — Compliance",
    )

    assert second.client_id == acme_support.client_id, "a duplicate client was created"

    from app.services import relay
    from app.services.gateway import FakeGateway

    item = await relay.open_request(
        session, FakeGateway(), source_chat=second, subject="KYC pack",
        body="Please send the updated pack.", raised_by_name="Tom Baker",
    )
    assert item.client_reference.startswith("ACME-")
    assert item.department is Department.COMPLIANCE
