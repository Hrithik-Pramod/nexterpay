"""The PRD's flow diagrams, executed step by step.

Each test walks one documented flow verbatim and asserts every stage happens.
If NexterPay ask "does it do what the document says", this file is the answer.

Section numbers refer to the Product Requirements Document v1.0 – Phase 1.
"""

from __future__ import annotations

import pytest

from app.bot.registry import register_client_chat, register_operations_chat, upsert_staff
from app.domain.enums import Department, Priority, StaffRole, WorkItemStatus
from app.domain.history import load_events, render_history
from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


# ---------------------------------------------------------------------------
# §8.2 Request routing — "no manual routing should be required"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("department", list(Department))
async def test_prd_8_2_routing_by_source_group(session, gw, department):
    """Support Group → Support Operations, and the same for the other three.

    Exercises all four departments, not just Support.
    """
    ops = await register_operations_chat(
        session,
        telegram_chat_id=-1000 - list(Department).index(department),
        department=department,
        title=f"{department.value.title()} Operations",
    )
    client_chat = await register_client_chat(
        session,
        telegram_chat_id=-2000 - list(Department).index(department),
        client_name="Acme Payments",
        department=department,
        title=f"Acme — {department.value.title()}",
    )

    item = await relay.open_request(
        session, gw,
        source_chat=client_chat,
        subject="Test request",
        body="Body of the request.",
        raised_by_name="Tom Baker",
    )

    assert item.department is department
    assert item.operations_chat_id == ops.id, "routed to the wrong department"
    assert item.topic_id in gw.topics[ops.telegram_chat_id]


async def test_prd_6_1_every_department_has_its_own_operations_group(session, gw):
    """§6.1 - each department has its own dedicated Operations Group.

    Counted from the enum rather than hard-coded, so adding a department
    tests the new one instead of failing on the old number. Compliance and
    Risk was the fifth.
    """
    ops_ids = {}
    for index, department in enumerate(Department):
        chat = await register_operations_chat(
            session, telegram_chat_id=-3000 - index, department=department
        )
        ops_ids[department] = chat.id

    assert len(set(ops_ids.values())) == len(Department), (
        "departments must not share a group"
    )


# ---------------------------------------------------------------------------
# §4 Communication Flow
#   Client → Client Group → Bot → Work Item → Ops Group → Assigned Staff
#   → Internal Discussion → Bot Synchronises Reply → Client Group
# ---------------------------------------------------------------------------

async def test_prd_4_communication_flow(
    session, acme_support, support_ops, operator, senior, gw
):
    stages: list[str] = []

    # Client → Client Telegram Group → Operations Bot → Work Item Created
    item = await relay.open_request(
        session, gw,
        source_chat=acme_support,
        subject="Settlement missing for 3 March",
        body="We have not received settlement for 3 March.",
        raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )
    stages.append("work item created")

    # → Relevant Department Operations Group
    assert item.operations_chat_id == support_ops.id
    assert item.display_reference in gw.all_text_to(OPS_CHAT)
    stages.append("routed to operations group")

    # → Assigned Staff Member
    await relay.assign(session, gw, item, operator, Actor.of(senior))
    assert item.owner_staff_id == operator.id
    stages.append("assigned")

    # → Internal Discussion
    await relay.add_internal_note(
        session, gw, item, Actor.of(operator), "Checking the settlement file."
    )
    stages.append("internal discussion")

    # → Bot Synchronises Reply → Client Telegram Group
    await relay.send_client_reply(
        session, gw, item, Actor.of(operator), "we are checking this now."
    )
    assert any("we are checking this now." in m for m in gw.messages_to(CLIENT_CHAT))
    stages.append("reply synchronised to client")

    assert stages == [
        "work item created",
        "routed to operations group",
        "assigned",
        "internal discussion",
        "reply synchronised to client",
    ]

    # The internal discussion did not travel with it.
    assert "settlement file" not in gw.all_text_to(CLIENT_CHAT)


# ---------------------------------------------------------------------------
# §3.5 Operational Consistency
#   Client Request → Work Item → Assigned → Worked Internally
#   → Client Updated → Completed
# ---------------------------------------------------------------------------

async def test_prd_3_5_operational_consistency(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Login issue",
        body="Cannot log in.", raised_by_name="Tom Baker",
    )
    assert item.status is WorkItemStatus.OPEN

    await relay.claim(session, gw, item, Actor.of(operator))
    assert item.status is WorkItemStatus.IN_PROGRESS  # claiming starts the work

    await relay.send_client_reply(session, gw, item, Actor.of(operator), "resolved now.")

    await relay.change_status(session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator))
    assert item.status is WorkItemStatus.COMPLETED


# ---------------------------------------------------------------------------
# §10.2 Standard Workflow
#   New → Assigned → Internal Review → Client Response (where required)
#   → Further Internal Discussion (if required) → Resolved → Closed
# ---------------------------------------------------------------------------

async def test_prd_10_2_standard_workflow(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Payment query",
        body="Where is our payment?", raised_by_name="Tom Baker",
    )

    await relay.claim(session, gw, item, Actor.of(operator))                    # Assigned
    await relay.change_status(                                                  # Internal Review
        session, gw, item, WorkItemStatus.IN_PROGRESS, Actor.of(operator)
    )
    await relay.send_client_reply(                                              # Client Response
        session, gw, item, Actor.of(operator), "could you confirm the amount?"
    )
    await relay.change_status(
        session, gw, item, WorkItemStatus.WAITING_CLIENT, Actor.of(operator)
    )
    await relay.relay_client_message(                                           # client responds
        session, gw, item, text="£42,000.", sender_name="Tom Baker",
        telegram_message_id=500,
    )
    await relay.add_internal_note(                                              # Further discussion
        session, gw, item, Actor.of(senior), "Matches our records."
    )
    await relay.change_status(                                                  # Resolved
        session, gw, item, WorkItemStatus.COMPLETED, Actor.of(operator)
    )
    await relay.close(session, gw, item, Actor.of(operator))                     # Closed

    assert item.status is WorkItemStatus.CLOSED

    trail = "\n".join(render_history(await load_events(session, item)))
    for stage in ["Claimed by", "In Progress", "Reply sent to client",
                  "Waiting for Client", "Message received from",
                  "Internal note by", "Completed", "Closed by"]:
        assert stage in trail, f"stage missing from the trail: {stage}"


# ---------------------------------------------------------------------------
# §15.1–15.3 Support / Finance / Development
#   Client Group → Raise Request → Work Item → Ops Group → Claimed
#   → Internal Discussion → Client Updated → Resolved → Closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "department",
    [Department.SUPPORT, Department.FINANCE, Department.DEVELOPMENT],
)
async def test_prd_15_departmental_workflow(session, gw, department):
    index = list(Department).index(department)
    ops = await register_operations_chat(
        session, telegram_chat_id=-4000 - index, department=department
    )
    client_chat = await register_client_chat(
        session, telegram_chat_id=-5000 - index,
        client_name="Acme Payments", department=department,
    )
    person = await upsert_staff(
        session, telegram_user_id=6000 + index, display_name="Dana Reed",
        role=StaffRole.SENIOR_OPERATOR, department=department,
    )

    item = await relay.open_request(
        session, gw, source_chat=client_chat,
        subject=f"{department.value} request",
        body="Please assist.", raised_by_name="Tom Baker",
    )
    await relay.claim(session, gw, item, Actor.of(person))
    await relay.add_internal_note(session, gw, item, Actor.of(person), "Looking into it.")
    await relay.send_client_reply(session, gw, item, Actor.of(person), "an update for you.")
    await relay.change_status(session, gw, item, WorkItemStatus.COMPLETED, Actor.of(person))
    await relay.close(session, gw, item, Actor.of(person))

    assert item.status is WorkItemStatus.CLOSED
    assert (ops.telegram_chat_id, item.topic_id) in gw.closed_topics
    assert "Looking into it." not in gw.all_text_to(client_chat.telegram_chat_id)


# ---------------------------------------------------------------------------
# §15.4 Business Operations
#   Commercial Enquiry → Work Item → Claimed → Qualification
#   → Commercial Discussion → Proposal Sent → Awaiting Client
#   → Agreed/Declined → Closed
# ---------------------------------------------------------------------------

async def test_prd_15_4_business_enquiry_is_free_format(session, gw):
    """The parts of §15.4 that are built: free-format capture and the fields
    the bot must record."""
    await register_operations_chat(
        session, telegram_chat_id=-7001, department=Department.BUSINESS
    )
    business_chat = await register_client_chat(
        session, telegram_chat_id=-7002, client_name="Acme Payments",
        department=Department.BUSINESS,
    )

    enquiry = "We are looking to add South Africa. Can you provide pricing?"
    item = await relay.open_request(
        session, gw, source_chat=business_chat,
        subject=enquiry[:120], body=enquiry,
        raised_by_name="Tom Baker", raised_by_telegram_user_id=9001,
    )

    # §15.4 lists what the Bot must capture automatically.
    assert item.client_id is not None                     # Client Name
    assert item.source_chat_id == business_chat.id        # Source Business Group
    assert item.raised_by_name == "Tom Baker"             # Person Raising
    assert item.created_at is not None                    # Date & Time Raised
    assert item.original_message == enquiry               # stored exactly as submitted
    assert item.display_reference.startswith("#")         # Unique Reference


def test_prd_15_4_business_button_wording():
    from app.bot import keyboards as kb

    assert kb.raise_request_prompt("business").inline_keyboard[0][0].text == (
        "Commercial Enquiry"
    )


async def test_prd_15_4_stage_model_is_not_implemented(session, gw):
    """Documents a known divergence rather than hiding it.

    §15.4 describes Qualification → Commercial Discussion → Proposal Sent →
    Awaiting Client → Agreed/Declined. §11 defines a single status list for all
    departments. The two conflict, so Business currently uses §11 and this test
    will fail the moment someone adds the §15.4 stages without also updating
    the client on which model was chosen.
    """
    stage_names = {s.value for s in WorkItemStatus}
    for stage in {"qualification", "proposal_sent", "agreed", "declined"}:
        assert stage not in stage_names, (
            "a §15.4 stage was added - confirm with NexterPay which model applies "
            "and update the README coverage table"
        )


# ---------------------------------------------------------------------------
# §9.3 Topic lifecycle, §7.2 Work Item information
# ---------------------------------------------------------------------------

async def test_prd_9_3_topic_archived_on_completion(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Anything",
        body="Anything.", raised_by_name="Tom Baker",
    )
    assert not gw.closed_topics

    await relay.close(session, gw, item, Actor.of(operator))
    assert (OPS_CHAT, item.topic_id) in gw.closed_topics


async def test_prd_7_2_all_fourteen_fields_present(
    session, acme_support, support_ops, operator, gw
):
    """§7.2 lists fourteen items every Work Item must hold as a minimum."""
    from sqlalchemy import select

    from app.db.models import Attachment, Message

    item = await relay.open_request(
        session, gw, source_chat=acme_support,
        subject="Settlement missing", body="Missing settlement.",
        raised_by_name="Tom Baker", raised_by_telegram_user_id=9001,
        attachments=[relay.IncomingAttachment(
            file_id="F1", file_unique_id="u1", kind="document", file_name="s.pdf"
        )],
    )
    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.add_internal_note(session, gw, item, Actor.of(operator), "note")

    messages = (await session.execute(
        select(Message).where(Message.work_item_id == item.id)
    )).scalars().all()
    attachments = (await session.execute(
        select(Attachment).where(Attachment.work_item_id == item.id)
    )).scalars().all()

    assert item.reference                       # 1  Unique Reference Number
    assert item.department                      # 2  Department
    assert item.client_id                       # 3  Client Name
    assert item.source_chat_id                  # 4  Source Telegram Group
    assert item.raised_by_name                  # 5  Person Raising the Request
    assert item.created_at                      # 6  Date & Time Raised
    assert item.status                          # 7  Current Status
    assert item.priority                        # 8  Priority
    assert item.owner_staff_id                  # 9  Assigned Owner
    assert item.original_message                # 10 Original Client Message
    assert any(m.direction.value == "internal" for m in messages)   # 11 Internal Notes
    assert len(messages) > 1                    # 12 Conversation History
    assert attachments                          # 13 Attachments
    assert await load_events(session, item)     # 14 Audit History


# ---------------------------------------------------------------------------
# §13 Permissions
# ---------------------------------------------------------------------------

async def test_prd_13_permission_tiers(session, acme_support, support_ops, gw):
    """Operator may work items; the disruptive actions need seniority."""
    from app.domain.errors import NotAuthorised

    op = await upsert_staff(
        session, telegram_user_id=7101, display_name="Op",
        role=StaffRole.OPERATOR, department=Department.SUPPORT,
    )
    senior = await upsert_staff(
        session, telegram_user_id=7102, display_name="Senior",
        role=StaffRole.SENIOR_OPERATOR, department=Department.SUPPORT,
    )

    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="x",
        body="x", raised_by_name="Tom Baker",
    )

    # Operator: claim, reply, note, status, close — all permitted.
    await relay.claim(session, gw, item, Actor.of(op))
    await relay.send_client_reply(session, gw, item, Actor.of(op), "hello")
    await relay.add_internal_note(session, gw, item, Actor.of(op), "note")
    await relay.change_status(session, gw, item, WorkItemStatus.IN_PROGRESS, Actor.of(op))

    # Operator: reassign, escalate, priority — all refused.
    with pytest.raises(NotAuthorised):
        await relay.assign(session, gw, item, senior, Actor.of(op))
    with pytest.raises(NotAuthorised):
        await relay.change_status(session, gw, item, WorkItemStatus.ESCALATED, Actor.of(op))
    with pytest.raises(NotAuthorised):
        await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(op))

    # Senior: all three permitted.
    await relay.assign(session, gw, item, op, Actor.of(senior))
    await relay.change_status(session, gw, item, WorkItemStatus.ESCALATED, Actor.of(senior))
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(senior))


# ---------------------------------------------------------------------------
# §7.3 "Ownership must be clearly visible to all members of the Operations Group"
# ---------------------------------------------------------------------------

async def test_prd_7_3_header_shows_current_owner(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    header_id = item.header_message_id
    assert header_id is not None

    await relay.claim(session, gw, item, Actor.of(operator))
    assert "Owner: Sarah Hill" in gw.current_text(header_id)

    await relay.assign(session, gw, item, senior, Actor.of(senior))
    assert "Owner: James Okoro" in gw.current_text(header_id)


async def test_prd_7_3_header_tracks_status_and_priority(
    session, acme_support, support_ops, operator, senior, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    header_id = item.header_message_id

    await relay.change_status(session, gw, item, WorkItemStatus.ESCALATED, Actor.of(senior))
    await relay.change_priority(session, gw, item, Priority.CRITICAL, Actor.of(senior))

    header = gw.current_text(header_id)
    assert "Status: Escalated" in header
    assert "Priority: Critical" in header


async def test_prd_7_3_header_reflects_closure(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    header_id = item.header_message_id

    await relay.claim(session, gw, item, Actor.of(operator))
    await relay.close(session, gw, item, Actor.of(operator))

    assert "Status: Closed" in gw.current_text(header_id)


async def test_header_edits_never_reach_the_client(
    session, acme_support, support_ops, operator, gw
):
    item = await relay.open_request(
        session, gw, source_chat=acme_support, subject="Settlement",
        body="Missing.", raised_by_name="Tom Baker",
    )
    await relay.claim(session, gw, item, Actor.of(operator))

    edits_to_client = [
        c for c in gw.calls
        if c.method == "edit_message_text" and c.chat_id == CLIENT_CHAT
    ]
    assert edits_to_client == []
