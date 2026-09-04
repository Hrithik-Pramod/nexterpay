"""Broadcasting — the highest-reach action on the platform.

Every other action touches one group. This one touches all of them, and after
48 hours it cannot be taken back. These tests exist to hold the four
guarantees NexterPay agreed to: Manager and above, nothing without
confirmation, every recipient recorded including failures, and recallable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.errors import DomainError, NotAuthorised
from app.domain.work_items import Actor
from app.services import broadcast as bc
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
SUPPLIER_CHAT = -1003000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


# --------------------------------------------------------------------------
# Who may send one
# --------------------------------------------------------------------------


async def test_an_operator_cannot_broadcast(
    session, acme_support, support_ops, operator, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    with pytest.raises(NotAuthorised):
        await bc.send(
            session, gw, body="scheduled maintenance", audience=bc.EVERYONE,
            recipients=recipients, actor=Actor.of(operator),
        )
    assert gw.messages_to(CLIENT_CHAT) == []


async def test_a_senior_operator_cannot_broadcast_either(
    session, acme_support, support_ops, senior, gw
):
    """Deliberately higher than reassignment.

    Reassigning one ticket needs Senior Operator. Messaging every client at
    once should not be available on the same authority.
    """
    recipients = await bc.audience_for(session, bc.EVERYONE)
    with pytest.raises(NotAuthorised):
        await bc.send(
            session, gw, body="hello", audience=bc.EVERYONE,
            recipients=recipients, actor=Actor.of(senior),
        )


async def test_a_manager_can(session, acme_support, support_ops, manager, gw):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="scheduled maintenance Saturday", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )
    assert record.id is not None
    assert "scheduled maintenance Saturday" in gw.all_text_to(CLIENT_CHAT)


# --------------------------------------------------------------------------
# Who it reaches
# --------------------------------------------------------------------------


async def test_operations_groups_are_never_recipients(
    session, acme_support, support_ops, pexi_supplier
):
    """Staff already read the group a broadcast is composed in."""
    for audience in (bc.EVERYONE, bc.CLIENTS, bc.SUPPLIERS):
        reached = {r.telegram_chat_id for r in await bc.audience_for(session, audience)}
        assert OPS_CHAT not in reached, f"{audience} included an Operations Group"


async def test_targeting_separates_clients_from_suppliers(
    session, acme_support, support_ops, pexi_supplier
):
    clients = {r.telegram_chat_id for r in await bc.audience_for(session, bc.CLIENTS)}
    suppliers = {r.telegram_chat_id for r in await bc.audience_for(session, bc.SUPPLIERS)}
    everyone = {r.telegram_chat_id for r in await bc.audience_for(session, bc.EVERYONE)}

    assert clients == {CLIENT_CHAT}
    assert suppliers == {SUPPLIER_CHAT}
    assert everyone == {CLIENT_CHAT, SUPPLIER_CHAT}
    assert not (clients & suppliers), "a group cannot be in both"


async def test_selected_reaches_only_those_chosen(
    session, acme_support, support_ops, pexi_supplier
):
    chosen = await bc.audience_for(session, bc.SELECTED, [SUPPLIER_CHAT])
    assert {r.telegram_chat_id for r in chosen} == {SUPPLIER_CHAT}


async def test_an_inactive_group_is_not_reached(
    session, acme_support, support_ops, pexi_supplier
):
    acme_support.is_active = False
    await session.flush()
    reached = {r.telegram_chat_id for r in await bc.audience_for(session, bc.EVERYONE)}
    assert CLIENT_CHAT not in reached


# --------------------------------------------------------------------------
# The confirmation step
# --------------------------------------------------------------------------


async def test_the_preview_states_the_reach_before_anything_is_sent(
    session, acme_support, support_ops, pexi_supplier, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    text = bc.preview("systems down 02:00-04:00", bc.EVERYONE, recipients)

    assert "2 groups" in text
    assert "Acme" in text and "Pexi" in text
    assert "systems down 02:00-04:00" in text
    assert "Nothing has been sent yet" in text
    # Composing a preview must not send anything.
    assert gw.calls == []


async def test_an_empty_audience_is_refused(
    session, acme_support, support_ops, manager, gw
):
    with pytest.raises(DomainError):
        await bc.send(
            session, gw, body="hello", audience=bc.SELECTED,
            recipients=[], actor=Actor.of(manager),
        )


async def test_an_empty_message_is_refused(
    session, acme_support, support_ops, manager, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    with pytest.raises(DomainError):
        await bc.send(
            session, gw, body="   ", audience=bc.EVERYONE,
            recipients=recipients, actor=Actor.of(manager),
        )


# --------------------------------------------------------------------------
# Recording what actually happened
# --------------------------------------------------------------------------


async def test_a_group_that_fails_is_recorded_not_swallowed(
    session, acme_support, support_ops, pexi_supplier, manager, gw
):
    """A bot removed from a group fails quietly.

    "It went to everyone" would then be a lie, which is worse than a partial
    failure you can see.
    """
    real_send = gw.send_message

    async def fail_for_the_supplier(chat_id, text, **kwargs):
        if chat_id == SUPPLIER_CHAT:
            raise RuntimeError("bot was removed from the group")
        return await real_send(chat_id, text, **kwargs)

    gw.send_message = fail_for_the_supplier

    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="notice", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )
    deliveries = await bc.deliveries_for(session, record)

    failed = [d for d in deliveries if d.error]
    assert len(failed) == 1
    assert failed[0].telegram_chat_id == SUPPLIER_CHAT
    assert "removed from the group" in failed[0].error

    summary = bc.outcome(record, deliveries)
    assert "1 of 2" in summary
    assert "Did not arrive" in summary
    # And the other group still received it.
    assert "notice" in gw.all_text_to(CLIENT_CHAT)


# --------------------------------------------------------------------------
# Taking it back
# --------------------------------------------------------------------------


async def test_recall_deletes_what_was_sent(
    session, acme_support, support_ops, pexi_supplier, manager, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="ignore this, sent in error", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )

    result = await bc.recall(session, gw, record, Actor.of(manager))

    assert "Removed from 2 groups" in result
    assert {c for c, _ in gw.deleted} == {CLIENT_CHAT, SUPPLIER_CHAT}
    assert record.recalled_at is not None


async def test_recalling_twice_does_nothing_the_second_time(
    session, acme_support, support_ops, manager, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="oops", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )
    await bc.recall(session, gw, record, Actor.of(manager))
    deleted = len(gw.deleted)

    again = await bc.recall(session, gw, record, Actor.of(manager))
    assert "already been recalled" in again
    assert len(gw.deleted) == deleted


async def test_recall_is_refused_after_48_hours(
    session, acme_support, support_ops, manager, gw
):
    """Telegram will not delete a bot's own message after 48 hours.

    Better to say so plainly than report a success that did not happen.
    """
    from app.db.base import utcnow

    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="old news", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )
    record.created_at = utcnow() - timedelta(hours=49)
    await session.flush()

    with pytest.raises(DomainError) as caught:
        await bc.recall(session, gw, record, Actor.of(manager))
    assert "48 hours" in str(caught.value)


async def test_an_operator_cannot_recall(
    session, acme_support, support_ops, manager, operator, gw
):
    recipients = await bc.audience_for(session, bc.EVERYONE)
    record = await bc.send(
        session, gw, body="notice", audience=bc.EVERYONE,
        recipients=recipients, actor=Actor.of(manager),
    )
    with pytest.raises(NotAuthorised):
        await bc.recall(session, gw, record, Actor.of(operator))


# --------------------------------------------------------------------------
# Replying to one
# --------------------------------------------------------------------------


async def test_a_broadcast_message_is_recognisable_as_one(
    session, acme_support, support_ops, manager, gw
):
    """So a reply to it can open a new request rather than being lost."""
    recipients = await bc.audience_for(session, bc.CLIENTS)
    record = await bc.send(
        session, gw, body="maintenance notice", audience=bc.CLIENTS,
        recipients=recipients, actor=Actor.of(manager),
    )
    delivery = (await bc.deliveries_for(session, record))[0]

    assert await bc.was_broadcast(session, CLIENT_CHAT, delivery.telegram_message_id)
    assert not await bc.was_broadcast(session, CLIENT_CHAT, 999999)
    assert not await bc.was_broadcast(session, OPS_CHAT, delivery.telegram_message_id)


async def test_a_request_from_a_broadcast_says_so_in_the_topic(
    session, acme_support, support_ops, manager, gw
):
    """Whoever picks it up must be able to see what it was a reply to.

    Reported during testing: a client replied "why so?" to a broadcast and the
    resulting topic showed only those two words. Nobody reading it could tell
    what it was about.
    """
    from app.bot.handlers.client import _broadcast_context
    from app.services import relay

    recipients = await bc.audience_for(session, bc.CLIENTS)
    record = await bc.send(
        session, gw, body="Scheduled maintenance Saturday 02:00 to 04:00.",
        audience=bc.CLIENTS, recipients=recipients, actor=Actor.of(manager),
    )
    delivery = (await bc.deliveries_for(session, record))[0]

    behind = await bc.broadcast_behind(
        session, CLIENT_CHAT, delivery.telegram_message_id
    )
    assert behind is not None and behind.id == record.id

    await relay.open_request(
        session, gw, source_chat=acme_support, subject="why so?",
        body="why so?", raised_by_name="Charley",
        context=_broadcast_context(behind),
    )

    topic = gw.all_text_to(OPS_CHAT)
    assert "Raised in reply to the broadcast" in topic
    assert "Scheduled maintenance Saturday" in topic
    assert manager.display_name in topic


async def test_the_client_is_not_told_about_the_context(
    session, acme_support, support_ops, manager, gw
):
    """The context line is for the team. It is not sent outward."""
    from app.services import relay

    before = len(gw.messages_to(CLIENT_CHAT))
    await relay.open_request(
        session, gw, source_chat=acme_support, subject="why so?",
        body="why so?", raised_by_name="Charley",
        context="↳ Raised in reply to the broadcast sent 31 Aug by Priya Nair",
    )
    # Only the acknowledgement, not the context line.
    new = gw.messages_to(CLIENT_CHAT)[before:]
    assert len(new) == 1
    assert "Raised in reply to the broadcast" not in new[0]


# --------------------------------------------------------------------------
# The handler, not just the service.
#
# Every test above this line calls app.services.broadcast directly. That is
# why NexterPay could report "broadcast did not work" while all of them
# passed: the fault was in the handler, one layer up, in the step that opens
# the composer. A feature is not covered until something exercises the path a
# person actually takes.
# --------------------------------------------------------------------------

async def test_a_manager_is_offered_a_composer_that_opens(
    session, support_ops, acme_support, manager, gw, monkeypatch
):
    """The bug behind "it did nothing".

    ForceReply(selective=True) forces a reply from the users *mentioned* in
    the message. The prompt named nobody, so it opened for nobody: the text
    appeared, no composer, and the person concluded the feature was broken.
    Nothing was logged because nothing failed.
    """
    import contextlib
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import deps
    from app.bot.handlers import broadcast as handlers

    deps.set_gateway(gw)

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(handlers, "session_scope", fake_scope)

    sent: list[tuple[str, dict]] = []

    async def reply(text, **kwargs):
        sent.append((text, kwargs))

    message = SimpleNamespace(
        chat=SimpleNamespace(id=support_ops.telegram_chat_id),
        from_user=SimpleNamespace(
            id=manager.telegram_user_id, full_name=manager.display_name
        ),
        reply=reply,
    )
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=support_ops.telegram_chat_id, user_id=1),
    )

    await handlers.start(message, state)

    assert sent, "the manager got no reply at all"
    text, kwargs = sent[-1]
    markup = kwargs.get("reply_markup")

    assert markup is not None and markup.force_reply, "no composer was offered"
    if markup.selective:
        # Selective is only honoured for users mentioned in the text, and a
        # tg://user link is the only thing that counts as a mention.
        assert f'tg://user?id={manager.telegram_user_id}' in text, (
            "selective is on but nobody is mentioned - this opens for nobody"
        )
        assert kwargs.get("parse_mode") == "HTML", "the mention will render as text"

    assert await state.get_state() == "BroadcastCompose:awaiting_message"


async def test_an_operator_is_told_why_rather_than_ignored(
    session, support_ops, operator, gw, monkeypatch
):
    """Broadcasting is Manager and above. Being refused is correct - being
    refused silently is not, and is indistinguishable from a fault."""
    import contextlib
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import deps
    from app.bot.handlers import broadcast as handlers

    deps.set_gateway(gw)

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(handlers, "session_scope", fake_scope)

    sent: list[str] = []

    async def reply(text, **kwargs):
        sent.append(text)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=support_ops.telegram_chat_id),
        from_user=SimpleNamespace(
            id=operator.telegram_user_id, full_name=operator.display_name
        ),
        reply=reply,
    )
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=support_ops.telegram_chat_id, user_id=2),
    )

    await handlers.start(message, state)

    assert sent, "an operator was refused with no explanation"
    assert "manager" in sent[-1].lower()
    assert await state.get_state() is None, "state was set for someone who was refused"
