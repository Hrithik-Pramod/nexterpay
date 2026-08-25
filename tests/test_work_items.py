"""Work item lifecycle and permissions."""

from __future__ import annotations

import pytest

from app.db.models import Chat
from app.domain import work_items as wi
from app.domain.enums import EventType, Priority, WorkItemStatus
from app.domain.errors import AlreadyOwned, NotAuthorised, WorkItemClosed
from app.domain.history import load_events
from app.domain.work_items import Actor


async def _raise_request(session, chat: Chat, subject="Payment not settled"):
    return await wi.create_work_item(
        session,
        source_chat=chat,
        subject=subject,
        original_message="We are missing settlement for 3 March.",
        raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )


async def test_creation_routes_by_source_group(session, acme_support, support_ops):
    item = await _raise_request(session, acme_support)

    assert item.department == acme_support.department
    assert item.operations_chat_id == support_ops.id
    assert item.status is WorkItemStatus.OPEN
    assert item.priority is Priority.MEDIUM
    assert item.display_reference.startswith("#")


async def test_references_are_sequential_and_unique(session, acme_support, support_ops):
    first = await _raise_request(session, acme_support)
    second = await _raise_request(session, acme_support)

    assert second.reference == first.reference + 1


async def test_claim_sets_owner_and_status(session, acme_support, support_ops, operator):
    item = await _raise_request(session, acme_support)
    await wi.claim(session, item, Actor.of(operator))

    assert item.owner_staff_id == operator.id
    assert item.status is WorkItemStatus.CLAIMED


async def test_second_claim_is_refused(session, acme_support, support_ops, operator, senior):
    item = await _raise_request(session, acme_support)
    await wi.claim(session, item, Actor.of(operator))

    with pytest.raises(AlreadyOwned):
        await wi.claim(session, item, Actor.of(senior))


async def test_operator_cannot_reassign(session, acme_support, support_ops, operator, senior):
    item = await _raise_request(session, acme_support)

    with pytest.raises(NotAuthorised):
        await wi.assign(session, item, senior, Actor.of(operator))


async def test_senior_can_reassign(session, acme_support, support_ops, operator, senior):
    item = await _raise_request(session, acme_support)
    await wi.assign(session, item, operator, Actor.of(senior))

    assert item.owner_staff_id == operator.id


async def test_operator_cannot_change_priority(session, acme_support, support_ops, operator):
    item = await _raise_request(session, acme_support)

    with pytest.raises(NotAuthorised):
        await wi.change_priority(session, item, Priority.CRITICAL, Actor.of(operator))


async def test_operator_cannot_escalate(session, acme_support, support_ops, operator):
    item = await _raise_request(session, acme_support)

    with pytest.raises(NotAuthorised):
        await wi.change_status(session, item, WorkItemStatus.ESCALATED, Actor.of(operator))


async def test_deactivated_staff_are_refused(session, acme_support, support_ops, operator):
    from app.bot.registry import deactivate_staff

    item = await _raise_request(session, acme_support)
    await deactivate_staff(session, operator.telegram_user_id)

    with pytest.raises(NotAuthorised):
        await wi.claim(session, item, Actor.of(operator))


async def test_closed_item_rejects_changes(session, acme_support, support_ops, operator):
    item = await _raise_request(session, acme_support)
    await wi.claim(session, item, Actor.of(operator))
    await wi.close(session, item, Actor.of(operator))

    assert item.status is WorkItemStatus.CLOSED
    assert item.closed_at is not None

    with pytest.raises(WorkItemClosed):
        await wi.change_status(session, item, WorkItemStatus.IN_PROGRESS, Actor.of(operator))


async def test_reopen_requires_manager(session, acme_support, support_ops, operator, manager):
    item = await _raise_request(session, acme_support)
    await wi.close(session, item, Actor.of(operator))

    with pytest.raises(NotAuthorised):
        await wi.reopen(session, item, Actor.of(operator))

    await wi.reopen(session, item, Actor.of(manager))
    assert item.status is WorkItemStatus.IN_PROGRESS


async def test_no_op_changes_emit_no_event(session, acme_support, support_ops, senior):
    item = await _raise_request(session, acme_support)
    before = len(await load_events(session, item))

    await wi.change_priority(session, item, Priority.MEDIUM, Actor.of(senior))

    assert len(await load_events(session, item)) == before


async def test_open_items_for_chat_excludes_closed(
    session, acme_support, support_ops, operator
):
    a = await _raise_request(session, acme_support, subject="First")
    b = await _raise_request(session, acme_support, subject="Second")
    await wi.close(session, a, Actor.of(operator))

    remaining = await wi.open_items_for_chat(session, acme_support)
    assert [i.id for i in remaining] == [b.id]


async def test_every_mutation_emits_exactly_one_event(
    session, acme_support, support_ops, senior
):
    item = await _raise_request(session, acme_support)
    await wi.claim(session, item, Actor.of(senior))
    await wi.change_status(session, item, WorkItemStatus.IN_PROGRESS, Actor.of(senior))
    await wi.change_priority(session, item, Priority.HIGH, Actor.of(senior))
    await wi.close(session, item, Actor.of(senior))

    types = [e.event_type for e in await load_events(session, item)]

    assert types == [
        EventType.WORK_ITEM_CREATED,
        EventType.OWNERSHIP_CLAIMED,
        EventType.STATUS_CHANGED,
        EventType.PRIORITY_CHANGED,
        EventType.WORK_ITEM_CLOSED,
    ]
