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


# --------------------------------------------------------------------------
# Not in front of the counterparty
#
# NexterPay, 5 September: a client typing an administrator command in their
# own group should get nothing back. They were right, and /npleads was worse
# than the case they found - it had no permission check at all, so a client
# could ask who NexterPay had named as their contacts and be told.
#
# This does not contradict "a refusal must speak". That rule exists because a
# colleague cannot tell silence apart from a fault. A client is not
# troubleshooting our bot; they are being handed our internal mechanism.
# --------------------------------------------------------------------------


def _admin_source():
    import inspect

    from app.bot.handlers import admin

    return inspect.getsource(admin._admin_or_refuse)


def test_the_refusal_asks_whose_room_it_is() -> None:
    source = _admin_source()
    assert "ChatKind.OPERATIONS" in source, (
        "_admin_or_refuse does not distinguish our own group from a "
        "counterparty's"
    )
    assert "return False" in source


def test_an_unregistered_group_counts_as_outside() -> None:
    """We have no idea who is in it, so it gets the quiet treatment."""
    source = _admin_source()
    assert "internal = chat is not None and chat.kind is ChatKind.OPERATIONS" in source, (
        "an unregistered group must not be treated as internal by default"
    )


def test_leads_is_no_longer_open_to_anyone() -> None:
    """The specific hole. It had no check of any kind."""
    import inspect

    from app.bot.handlers import admin

    source = inspect.getsource(admin.cmd_leads)
    assert "_admin_or_refuse" in source, "/npleads still answers whoever asks"


@pytest.mark.parametrize(
    "name",
    ["cmd_setlead", "cmd_leads", "cmd_removelead"],
)
def test_every_lead_command_is_guarded(name: str) -> None:
    """All three, because they are the ones that live in a counterparty group
    and are therefore the ones a client can reach."""
    import inspect

    from app.bot.handlers import admin

    source = inspect.getsource(getattr(admin, name))
    assert "_admin_or_refuse" in source, f"{name} is unguarded"


# --------------------------------------------------------------------------
# Finding the contact from the Operations side
#
# NexterPay, 5 September: "once leads are set, in our operations groups, how
# do we look up the lead name?" There was no answer. The only way was to walk
# into the counterparty's own group and run /npleads there - which is exactly
# the trip the header exists to save - and running it in an Operations Group
# looked up that room's own leads and confidently replied "nobody is named
# for this group yet".
# --------------------------------------------------------------------------


async def test_the_header_names_the_contact(
    session, acme_support, support_ops, operator, gw
):
    from app.bot.registry import set_group_lead

    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    await set_group_lead(
        session, acme_support, telegram_user_id=7788, display_name="Gavs D",
    )
    await relay.refresh_header(session, gw, item)

    header = gw.current_text(item.header_message_id)
    assert "Contact" in header, "the header does not name the contact"
    assert "Gavs D" in header
    assert "tg://user?id=7788" in header, "the contact is not tappable"


async def test_no_contact_line_when_nobody_is_named(
    session, acme_support, support_ops, gw
):
    """A permanent "Contact: none" is a line of noise on every header to save
    a moment's thought on a few - the same reasoning as Linked."""
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    await relay.refresh_header(session, gw, item)

    assert "Contact" not in gw.current_text(item.header_message_id)


async def test_the_contact_follows_the_group_not_the_client(
    session, acme_support, acme_compliance, support_ops, gw
):
    """A client with a Support group and a Compliance group usually has a
    different person in each, which is why leads are per chat."""
    from app.bot.registry import set_group_lead

    support_item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="A", body="a",
        raised_by_name="Tom",
    )
    await set_group_lead(
        session, acme_support, telegram_user_id=1, display_name="Support Sam",
    )
    await set_group_lead(
        session, acme_compliance, telegram_user_id=2, display_name="Compliance Chris",
    )
    await relay.refresh_header(session, gw, support_item)

    header = gw.current_text(support_item.header_message_id)
    assert "Support Sam" in header
    assert "Compliance Chris" not in header, "the wrong group's contact was shown"


def test_leads_answers_about_the_counterparty_from_a_topic() -> None:
    """Sent inside a request, it must answer about that request's
    counterparty rather than about the Operations Group it was typed in."""
    import inspect

    from app.bot.handlers import admin

    source = inspect.getsource(admin.cmd_leads)
    assert "work_item_for_thread" in source, (
        "/npleads still answers about whichever chat it was sent in"
    )
    assert "ChatKind.OPERATIONS" in source


# --------------------------------------------------------------------------
# Tagging the contact when we open the conversation
#
# NexterPay, 5 September, asking where the choice fits: "/npnewcl would show
# Acme in a drop down, after that if we have an option for group or name of
# lead, so the message is tagged at client end - possible?"
#
# It fits after the group is picked and the message typed, because until then
# there is no lead to offer: the contact belongs to the group, so it cannot be
# known before one is chosen.
# --------------------------------------------------------------------------


async def test_raising_outbound_can_address_the_contact(
    session, acme_support, support_ops, operator, gw
):
    from app.bot.registry import set_group_lead

    await set_group_lead(
        session, acme_support, telegram_user_id=7788, display_name="Gavs D",
    )
    await relay.open_outbound(
        session, gw, counterparty_chat=acme_support,
        subject="Reconciliation", body="Checking in on the March file.",
        actor=Actor.of(operator), tag_lead=True,
    )

    to_client = gw.all_text_to(acme_support.telegram_chat_id)
    assert "tg://user?id=7788" in to_client, "the contact was not tagged"
    assert "Checking in on the March file." in to_client


async def test_it_still_goes_to_the_room_by_default(
    session, acme_support, support_ops, operator, gw
):
    """Off unless asked for. Tagging the same person on everything teaches
    them to ignore it, which costs more than it buys."""
    from app.bot.registry import set_group_lead

    await set_group_lead(
        session, acme_support, telegram_user_id=7788, display_name="Gavs D",
    )
    await relay.open_outbound(
        session, gw, counterparty_chat=acme_support,
        subject="Reconciliation", body="Checking in.", actor=Actor.of(operator),
    )

    assert "tg://user" not in gw.all_text_to(acme_support.telegram_chat_id)


async def test_asking_to_tag_nobody_still_sends(
    session, acme_support, support_ops, operator, gw
):
    """A group with no named contact must not swallow the message. The
    button is not offered there, but the flag can still arrive - a lead
    removed between the preview and the tap is enough."""
    await relay.open_outbound(
        session, gw, counterparty_chat=acme_support,
        subject="Reconciliation", body="Checking in.", actor=Actor.of(operator),
        tag_lead=True,
    )
    assert "Checking in." in gw.all_text_to(acme_support.telegram_chat_id)


def test_the_tag_button_is_only_offered_where_someone_is_named() -> None:
    """A button that would do nothing needs explaining, and explaining it is
    worse than not offering it."""
    from app.bot.handlers.outbound import _confirm

    class _Lead:
        display_name = "Gavs D"
        telegram_user_id = 7788

    plain = [b.text for row in _confirm().inline_keyboard for b in row]
    tagged = [b.text for row in _confirm(_Lead()).inline_keyboard for b in row]

    assert not any("tag" in t for t in plain)
    assert any("Gavs D" in t for t in tagged)
    # Cancel survives both, or there is no way out of the preview.
    assert "Cancel" in plain and "Cancel" in tagged
