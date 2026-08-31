"""Compliance and Risk, the fifth department.

NexterPay's brief: requests come from suppliers, clients and themselves,
filed under the same Client / Supplier / Ticket structure, mostly KYC
requests and compliance notes for action, standard statuses, and — when
asked which buttons to remove — "leave for team, I don't think there will be
any changes".

So it is a full department with nothing trimmed. These tests hold that.
"""

from __future__ import annotations

import pytest

from app.bot.keyboards import work_item_actions
from app.bot.registry import register_client_chat, register_operations_chat
from app.domain.enums import Department, WorkItemStatus
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

COMPLIANCE_OPS = -1001000000009
COMPLIANCE_CLIENT = -1002000000009


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


def test_it_is_called_compliance_and_risk_not_compliance() -> None:
    """Titlecasing the stored value would give the wrong name."""
    assert Department.COMPLIANCE.label == "Compliance and Risk"
    assert Department.COMPLIANCE.value == "compliance"


def test_help_text_lists_it() -> None:
    """Generated from the enum, so it cannot go stale."""
    assert "compliance" in Department.usage()
    assert Department.usage().count("|") == len(Department) - 1


def test_the_department_value_fits_the_column() -> None:
    """The column is VARCHAR(11), sized to "development".

    A longer department name would need the column widening first, and would
    fail at insert time rather than at migration time - which is a nasty way
    to find out.
    """
    for department in Department:
        assert len(department.value) <= 11, (
            f"{department.value!r} will not fit the department column"
        )


async def test_a_full_request_lifecycle_works_in_compliance(session, gw, manager):
    """Nothing about the department is special, which is the point."""
    await register_operations_chat(
        session, telegram_chat_id=COMPLIANCE_OPS,
        department=Department.COMPLIANCE, title="Compliance Operations",
    )
    client_chat = await register_client_chat(
        session, telegram_chat_id=COMPLIANCE_CLIENT,
        client_name="Acme Payments Compliance",
        department=Department.COMPLIANCE, title="Acme — Compliance",
    )

    item = await relay.open_request(
        session, gw, source_chat=client_chat,
        subject="KYC documents for onboarding",
        body="Please send certified ID for the two new signatories.",
        raised_by_name="Tom Baker",
    )
    assert item.department is Department.COMPLIANCE

    await relay.claim(session, gw, item, Actor.of(manager))
    assert item.status is WorkItemStatus.IN_PROGRESS

    await relay.send_client_reply(
        session, gw, item, Actor.of(manager), "received, reviewing now."
    )
    await relay.close(session, gw, item, Actor.of(manager))
    assert item.status is WorkItemStatus.CLOSED

    # The header names the department properly.
    assert "Compliance and Risk" in gw.all_text_to(COMPLIANCE_OPS)


def test_nothing_is_trimmed_from_the_buttons() -> None:
    """NexterPay chose to keep the full set for now.

    If that changes, this is where it changes - deliberately, rather than by
    someone quietly dropping a button.
    """
    labels = [
        b.text for row in work_item_actions(1, claimed=False).inline_keyboard for b in row
    ]
    for expected in ("Claim", "Status", "Note", "Priority", "History", "Close"):
        assert any(expected in label for label in labels), f"{expected} missing"
    assert any("Reply to client" in label for label in labels)
    assert any("File under supplier" in label for label in labels)
