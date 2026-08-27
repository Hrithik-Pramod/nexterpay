"""The staff action buttons: Reply, Note and Reassign.

NexterPay chose a confirmation step over a one-tap send. These tests exist to
make sure that choice is real rather than decorative: the only line that may
ever put text into a client group is `sendreply`, and only with a draft that
belongs to the request in front of the person tapping it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import deps
from app.bot.handlers import staff as staff_handlers
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    fake = FakeGateway()
    deps.set_gateway(fake)
    return fake


@pytest.fixture
def state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OPS_CHAT, user_id=5001),
    )


def fake_query(thread_id: int | None = 55):
    """A stand-in for CallbackQuery that is as strict as the real thing.

    An earlier version accepted any keyword at all, which let a genuine bug
    through: `Message.answer()` fills in message_thread_id itself, so passing
    it explicitly raises TypeError in production. The fake swallowed it and
    the tests stayed green. A fake that is more permissive than the object it
    replaces is worse than no test.
    """

    sent: list[str] = []

    async def answer(text, **kwargs):
        if "message_thread_id" in kwargs:
            raise TypeError(
                "SendMessage() got multiple values for keyword argument "
                "'message_thread_id' - Message.answer() already sets it"
            )
        sent.append(text)
        return SimpleNamespace(message_id=1)

    async def edit_text(text, **kwargs):
        sent.append(f"EDIT:{text}")

    return SimpleNamespace(
        message=SimpleNamespace(
            message_thread_id=thread_id, answer=answer, edit_text=edit_text
        ),
        sent=sent,
    )


async def _item(session, gw, chat):
    return await relay.open_request(
        session, gw,
        source_chat=chat,
        subject="Settlement missing",
        body="No settlement for 3 March.",
        raised_by_name="Tom Baker",
    )


async def test_tapping_reply_sends_nothing_to_the_client(
    session, acme_support, support_ops, operator, gw, state
):
    item = await _item(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    query = fake_query()
    await staff_handlers._apply(
        session, query, "reply", None, item, Actor.of(operator), state
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert await state.get_state() == "StaffCompose:awaiting_reply"


async def test_cancel_discards_the_draft_and_sends_nothing(
    session, acme_support, support_ops, operator, gw, state
):
    item = await _item(session, gw, acme_support)
    await state.update_data(work_item_id=item.id, draft="internal wording, do not send")
    before = len(gw.messages_to(CLIENT_CHAT))

    await staff_handlers._apply(
        session, fake_query(), "cancelreply", None, item, Actor.of(operator), state
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert await state.get_data() == {}


async def test_send_delivers_the_draft_once(
    session, acme_support, support_ops, operator, gw, state
):
    item = await _item(session, gw, acme_support)
    await state.update_data(work_item_id=item.id, draft="Settlement released today.")

    await staff_handlers._apply(
        session, fake_query(), "sendreply", None, item, Actor.of(operator), state
    )
    assert "Settlement released today." in gw.all_text_to(CLIENT_CHAT)

    # A second tap on the same preview must not send it again.
    after_first = len(gw.messages_to(CLIENT_CHAT))
    note = await staff_handlers._apply(
        session, fake_query(), "sendreply", None, item, Actor.of(operator), state
    )
    assert len(gw.messages_to(CLIENT_CHAT)) == after_first
    assert "already been sent" in note


async def test_a_draft_from_another_request_is_never_sent(
    session, acme_support, support_ops, operator, gw, state
):
    """The dangerous case: two tickets open, the wrong preview tapped."""
    first = await _item(session, gw, acme_support)
    second = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Chargeback",
        body="Unrelated.", raised_by_name="Tom Baker",
    )
    await state.update_data(work_item_id=first.id, draft="About the settlement.")
    before = len(gw.messages_to(CLIENT_CHAT))

    note = await staff_handlers._apply(
        session, fake_query(), "sendreply", None, second, Actor.of(operator), state
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert "expired" in note or "already been sent" in note


def test_compose_handlers_run_before_the_topic_catch_all():
    """Same trap that hid the client-reply bug for a day.

    topic_message is filtered on message_thread_id, which matches any reply in
    a supergroup. If it were registered first it would consume the staff
    member's draft and the preview would never appear.
    """
    names = [h.callback.__name__ for h in staff_handlers.router.message.handlers]
    assert names.index("capture_reply_draft") < names.index("topic_message")
    assert names.index("capture_note_text") < names.index("topic_message")


# --------------------------------------------------------------------------
# Reassign
# --------------------------------------------------------------------------


async def test_reassign_is_refused_below_senior_operator(
    session, acme_support, support_ops, operator, senior, gw, state
):
    """An Operator sees the button, so the refusal has to be legible.

    The check runs before the list of colleagues is built, so they get one
    clear no rather than a menu where every option fails.
    """
    item = await _item(session, gw, acme_support)
    with pytest.raises(NotAuthorised):
        await staff_handlers._apply(
            session, fake_query(), "reassign", None, item, Actor.of(operator), state
        )


async def test_reassign_offers_colleagues_not_the_current_owner(
    session, acme_support, support_ops, operator, senior, manager, gw, state
):
    item = await _item(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    people = await staff_handlers._assignable(session, item)
    names = [p.display_name for p in people]

    assert "Sarah Hill" not in names          # already owns it
    assert "James Okoro" in names
    assert "Priya Nair" in names


async def test_setowner_hands_the_request_over(
    session, acme_support, support_ops, operator, senior, gw, state
):
    from app.domain.enums import WorkItemStatus

    item = await _item(session, gw, acme_support)

    note = await staff_handlers._apply(
        session, fake_query(), "setowner", str(operator.id), item,
        Actor.of(senior), state,
    )

    assert item.owner_staff_id == operator.id
    assert "Sarah Hill" in note
    # Handing it over starts it, exactly as claiming does.
    assert item.status is WorkItemStatus.IN_PROGRESS
    # And both facts reach the topic, not just the most recent one.
    topic = gw.all_text_to(OPS_CHAT)
    assert "Sarah Hill" in topic
    assert "Open → In Progress" in topic


async def test_setowner_refuses_an_inactive_person(
    session, acme_support, support_ops, operator, senior, gw, state
):
    item = await _item(session, gw, acme_support)
    operator.is_active = False
    await session.flush()

    note = await staff_handlers._apply(
        session, fake_query(), "setowner", str(operator.id), item,
        Actor.of(senior), state,
    )
    assert "no longer active" in note
    assert item.owner_staff_id != operator.id


async def test_note_button_prompts_without_touching_the_client(
    session, acme_support, support_ops, operator, gw, state
):
    item = await _item(session, gw, acme_support)
    before = len(gw.messages_to(CLIENT_CHAT))

    query = fake_query()
    await staff_handlers._apply(
        session, query, "note", None, item, Actor.of(operator), state
    )

    assert len(gw.messages_to(CLIENT_CHAT)) == before
    assert await state.get_state() == "StaffCompose:awaiting_note"
    assert any("stays in this group" in text for text in query.sent)


# --------------------------------------------------------------------------
# Closing
# --------------------------------------------------------------------------


async def test_closing_twice_does_not_tell_the_client_twice(
    session, acme_support, support_ops, operator, gw, state
):
    """The Close button stays on screen after the first tap.

    Regression from UAT. wi.close() was already idempotent, but relay.close()
    carried on regardless: the second tap sent the client another "your
    request has been closed" notice and then crashed on TOPIC_NOT_MODIFIED.
    The crash was the visible part; the duplicate message to the customer was
    the part that mattered.
    """
    item = await _item(session, gw, acme_support)
    await relay.close(session, gw, item, Actor.of(operator))

    client_messages = len(gw.messages_to(CLIENT_CHAT))
    closes = len(gw.closed_topics)

    await relay.close(session, gw, item, Actor.of(operator))

    assert len(gw.messages_to(CLIENT_CHAT)) == client_messages
    assert len(gw.closed_topics) == closes


async def test_a_closed_request_keeps_only_history(
    session, acme_support, support_ops, operator, gw, state
):
    item = await _item(session, gw, acme_support)

    query = fake_query()
    captured = {}

    async def edit_reply_markup(reply_markup=None, **kwargs):
        captured["markup"] = reply_markup

    query.message.edit_reply_markup = edit_reply_markup

    await staff_handlers._apply(
        session, query, "close", None, item, Actor.of(operator), state
    )

    labels = [b.text for row in captured["markup"].inline_keyboard for b in row]
    assert labels == ["History"]
