"""People who work more than one desk.

NexterPay confirmed during testing that they have staff who genuinely span two
departments. Until now registering someone for a second one silently removed
them from the first, and the workaround was to make them an administrator -
which grants configuring every group, registering clients and adding staff,
none of which "also helps out with Compliance" should imply.

The property these tests hold onto is that seniority is a fact about a person
*on a desk*. A Manager in Support who helps with Compliance must not be able
to reopen a Compliance ticket.
"""

from __future__ import annotations

import pytest

from app.bot import deps
from app.bot.registry import (
    register_operations_chat,
    remove_staff_from_department,
    resolve_staff,
    upsert_staff,
)
from app.domain.enums import Department, StaffRole
from app.domain.errors import NotAuthorised
from app.domain.work_items import Actor


@pytest.fixture
async def compliance_ops(session):
    return await register_operations_chat(
        session,
        telegram_chat_id=-1001000000009,
        department=Department.COMPLIANCE,
        title="Compliance Operations",
    )


async def _spanning(session) -> object:
    """A Manager in Support who also helps out in Compliance, as an Operator."""
    await upsert_staff(
        session, telegram_user_id=7001, display_name="Dana Ruiz",
        role=StaffRole.MANAGER, department=Department.SUPPORT,
    )
    return await upsert_staff(
        session, telegram_user_id=7001, display_name="Dana Ruiz",
        role=StaffRole.OPERATOR, department=Department.COMPLIANCE,
    )


async def test_a_second_department_is_added_not_swapped(session, support_ops):
    """The bug this whole change exists to fix."""
    person = await _spanning(session)

    assert set(person.departments) == {Department.SUPPORT, Department.COMPLIANCE}
    assert person.role_in(Department.SUPPORT) is StaffRole.MANAGER
    assert person.role_in(Department.COMPLIANCE) is StaffRole.OPERATOR


async def test_seniority_does_not_travel_between_desks(session):
    """A Manager in Support is an Operator in Compliance, and nothing else.

    Reopening needs Manager. If the role were read off the person rather than
    off the desk, they could reopen a Compliance ticket they have no standing
    on - which is exactly the over-granting the administrator workaround was
    already doing.
    """
    person = await _spanning(session)

    Actor.of(person, Department.SUPPORT).require(StaffRole.MANAGER)

    with pytest.raises(NotAuthorised):
        Actor.of(person, Department.COMPLIANCE).require(StaffRole.MANAGER)

    # But they are a full Operator there, not a bystander.
    Actor.of(person, Department.COMPLIANCE).require_any()


async def test_a_desk_they_do_not_work_refuses_them_differently(session):
    """"Not registered here" and "not senior enough" need different answers,
    because the fix for each is a different conversation."""
    person = await _spanning(session)
    actor = Actor.of(person, Department.FINANCE)

    assert actor.role is None
    with pytest.raises(NotAuthorised, match="not registered for this department"):
        actor.require_any()


async def test_an_ambiguous_actor_is_refused_rather_than_guessed(session):
    """Leaving the department out is fine for someone with one desk, and a
    question with no answer for someone with two. Guessing would either
    over-grant or wrongly refuse, so it raises instead."""
    person = await _spanning(session)

    with pytest.raises(ValueError, match="say which one"):
        Actor.of(person)


async def test_someone_with_one_desk_still_needs_no_department(session, operator):
    """The common case stays simple. If a person belongs to one department
    they can only be acting in it."""
    actor = Actor.of(operator)
    assert actor.role is StaffRole.OPERATOR


async def test_they_are_admitted_to_both_operations_groups(
    session, support_ops, compliance_ops
):
    await _spanning(session)

    support = await deps.staff_context(session, support_ops.telegram_chat_id, 7001)
    compliance = await deps.staff_context(session, compliance_ops.telegram_chat_id, 7001)

    assert support is not None and support[1].role is StaffRole.MANAGER
    assert compliance is not None and compliance[1].role is StaffRole.OPERATOR


async def test_a_department_they_do_not_work_still_turns_them_away(
    session, support_ops
):
    """Spanning two is not spanning all of them."""
    finance_ops = await register_operations_chat(
        session, telegram_chat_id=-1001000000008,
        department=Department.FINANCE, title="Finance Operations",
    )
    await _spanning(session)

    assert await deps.staff_context(session, finance_ops.telegram_chat_id, 7001) is None


async def test_the_refusal_names_the_desks_they_do_work(session, support_ops):
    """The old message said a person belongs to one department at a time.
    That is no longer true, and leaving it would send someone to ask for a
    move when what they want is to be added."""
    finance_ops = await register_operations_chat(
        session, telegram_chat_id=-1001000000008,
        department=Department.FINANCE, title="Finance Operations",
    )
    await _spanning(session)

    reason = await deps.refusal_reason(7001, session, finance_ops.telegram_chat_id)

    assert "Support" in reason and "Compliance" in reason
    assert "Finance" in reason
    assert "one department at a time" not in reason
    assert "more than" in reason, "it should say they can be added, not moved"


async def test_removing_one_desk_leaves_the_others(session):
    person = await _spanning(session)

    person, was_last = await remove_staff_from_department(
        session, 7001, Department.COMPLIANCE
    )

    assert was_last is False
    assert person.is_active is True
    assert person.departments == [Department.SUPPORT]
    assert person.role_in(Department.SUPPORT) is StaffRole.MANAGER


async def test_removing_the_last_desk_deactivates_them(session):
    """Otherwise they resolve as staff, work nowhere, and are refused
    everything with a message about seniority rather than about not being
    there - which sends them to the wrong person for a fix."""
    await upsert_staff(
        session, telegram_user_id=7002, display_name="Sam Idris",
        role=StaffRole.OPERATOR, department=Department.SUPPORT,
    )

    person, was_last = await remove_staff_from_department(
        session, 7002, Department.SUPPORT
    )

    assert was_last is True
    assert person.is_active is False
    assert await resolve_staff(session, 7002) is None


async def test_registering_the_same_desk_twice_updates_the_role(session):
    """A promotion, not a duplicate."""
    await upsert_staff(
        session, telegram_user_id=7003, display_name="Lee Park",
        role=StaffRole.OPERATOR, department=Department.SUPPORT,
    )
    person = await upsert_staff(
        session, telegram_user_id=7003, display_name="Lee Park",
        role=StaffRole.SENIOR_OPERATOR, department=Department.SUPPORT,
    )

    assert len(person.memberships) == 1
    assert person.role_in(Department.SUPPORT) is StaffRole.SENIOR_OPERATOR


async def test_an_administrator_still_works_everywhere(session, support_ops):
    """Administrators configure every department, so they are admitted to a
    group they do not belong to. That was true before and stays true - it is
    only the workaround for ordinary staff that this replaces."""
    finance_ops = await register_operations_chat(
        session, telegram_chat_id=-1001000000008,
        department=Department.FINANCE, title="Finance Operations",
    )
    await upsert_staff(
        session, telegram_user_id=7004, display_name="Root Admin",
        role=StaffRole.ADMINISTRATOR, department=Department.SUPPORT,
    )

    resolved = await deps.staff_context(session, finance_ops.telegram_chat_id, 7004)
    assert resolved is not None
    assert resolved[1].role is StaffRole.ADMINISTRATOR


async def test_reassignment_lists_someone_who_only_helps_out_here(
    session, acme_support, support_ops, compliance_ops, operator
):
    """The visible payoff. Somebody who spans two desks should be assignable
    on both, which the old model made impossible without over-promoting them.
    """
    from app.bot.handlers import staff as staff_handlers
    from app.services import relay
    from app.services.gateway import FakeGateway

    await _spanning(session)
    item = await relay.open_request(
        session, FakeGateway(), source_chat=acme_support,
        subject="Settlement", body="Not received.", raised_by_name="Tom Baker",
    )

    names = [p.display_name for p in await staff_handlers._assignable(session, item)]
    assert "Dana Ruiz" in names
