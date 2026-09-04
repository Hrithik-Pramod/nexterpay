"""Named contacts inside a counterparty group.

Telegram will not tell a bot who is in a group - there is no API call for it,
and that is Telegram's decision rather than an oversight. So NexterPay name
people instead, the same way staff are registered: reply to one of their
messages and the bot learns who they are.

One mechanism answering two requests - a lead per group, and "send this to a
person rather than to the room".
"""

from __future__ import annotations

import pytest

from app.bot.registry import leads_for, remove_group_lead, set_group_lead
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _named(session, chat, user_id=8001, name="Gavs D"):
    return await set_group_lead(
        session, chat, telegram_user_id=user_id, display_name=name
    )


async def test_a_group_with_no_lead_is_the_normal_case(session, acme_support):
    assert await leads_for(session, acme_support) == []


async def test_naming_someone_twice_updates_rather_than_collides(
    session, acme_support
):
    """People change their display name, and an administrator will re-run this
    rather than check whether they already did."""
    await _named(session, acme_support, name="Gavs")
    await _named(session, acme_support, name="Gavs D")

    leads = await leads_for(session, acme_support)
    assert len(leads) == 1
    assert leads[0].display_name == "Gavs D"


async def test_a_removed_lead_can_come_back(session, acme_support):
    """Deactivated rather than deleted, so re-adding does not hit the unique
    constraint - and a mention recorded earlier still resolves to a name."""
    await _named(session, acme_support)
    await remove_group_lead(session, acme_support, 8001)
    assert await leads_for(session, acme_support) == []

    await _named(session, acme_support)
    assert len(await leads_for(session, acme_support)) == 1


async def test_leads_are_per_group_not_per_client(
    session, acme_support, acme_compliance
):
    """A client with a Support group and a Compliance group usually has a
    different person in each, so this is held on the chat."""
    await _named(session, acme_support, user_id=8001, name="Gavs D")
    await _named(session, acme_compliance, user_id=8002, name="Priya N")

    assert [lead.display_name for lead in await leads_for(session, acme_support)] == ["Gavs D"]
    assert [lead.display_name for lead in await leads_for(session, acme_compliance)] == ["Priya N"]


async def test_a_tagged_reply_mentions_them_and_an_ordinary_one_does_not(
    session, acme_support, support_ops, operator, gw
):
    """The point of the feature: the named person is notified rather than
    relying on somebody in the group noticing."""
    lead = await _named(session, acme_support)
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )

    await relay.send_client_reply(session, gw, item, Actor.of(operator), "Looking now.")
    plain = gw.messages_to(CLIENT_CHAT)[-1]
    assert f"tg://user?id={lead.telegram_user_id}" not in plain

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Any update?", tag_lead=True
    )
    tagged = gw.messages_to(CLIENT_CHAT)[-1]
    assert f'tg://user?id={lead.telegram_user_id}' in tagged
    assert "Gavs D" in tagged
    assert "Any update?" in tagged


async def test_tagging_a_group_with_nobody_named_still_sends(
    session, acme_support, support_ops, operator, gw
):
    """A missing contact must not swallow the reply. The message matters more
    than the mention."""
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Still sent.", tag_lead=True
    )
    assert "Still sent." in gw.messages_to(CLIENT_CHAT)[-1]


async def test_a_tagged_reply_still_hides_the_supplier_code(
    session, acme_support, support_ops, operator, gw
):
    """The mention must not become a route round the confidentiality rule."""
    from app.db.models import Client

    client = await session.get(Client, acme_support.client_id)
    client.code = "ACME"
    await session.flush()

    await _named(session, acme_support)
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    supplier = Client(name="Supplier Pexi Ltd", code="SPEX")
    session.add(supplier)
    await session.flush()
    await relay.file_under(session, gw, item, supplier, Actor.of(operator))

    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "Chasing them now.", tag_lead=True
    )
    assert "SPEX" not in gw.all_text_to(CLIENT_CHAT)


async def test_staff_wording_is_escaped_in_a_tagged_reply(
    session, acme_support, support_ops, operator, gw
):
    """The tagged path is the only one that sends HTML, so it is the only one
    where a stray angle bracket in what somebody typed would be swallowed as
    markup or rejected outright by Telegram."""
    await _named(session, acme_support)
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator),
        "amount < 500 & rising <b>not bold</b>", tag_lead=True,
    )
    sent = gw.messages_to(CLIENT_CHAT)[-1]
    assert "amount &lt; 500 &amp; rising" in sent
    assert "&lt;b&gt;not bold&lt;/b&gt;" in sent
