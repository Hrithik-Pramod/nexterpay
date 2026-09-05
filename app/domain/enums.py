"""Domain enumerations.

These mirror the status and priority models defined in the NexterPay PRD
(sections 11 and 12) and the permission tiers in section 13.
"""

from __future__ import annotations

import enum


class Department(str, enum.Enum):
    SUPPORT = "support"
    FINANCE = "finance"
    DEVELOPMENT = "development"
    BUSINESS = "business"
    COMPLIANCE = "compliance"

    @property
    def label(self) -> str:
        """The name people use, which is not always the stored value.

        Compliance and Risk is the obvious case - titlecasing the value would
        give "Compliance", which is not what NexterPay call the department.
        """
        return {
            "support": "Support",
            "finance": "Finance",
            "development": "Development",
            "business": "Business",
            "compliance": "Compliance and Risk",
        }[self.value]

    @classmethod
    def usage(cls) -> str:
        """For help text, so adding a department never leaves a stale message."""
        return "|".join(d.value for d in cls)


class ChatKind(str, enum.Enum):
    """A registered Telegram group is either a client group or an internal
    Operations Group. The bot behaves very differently in each."""

    CLIENT = "client"
    OPERATIONS = "operations"


class WorkItemStatus(str, enum.Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    WAITING_CLIENT = "waiting_client"
    WAITING_INTERNAL = "waiting_internal"
    WAITING_THIRD_PARTY = "waiting_third_party"
    ESCALATED = "escalated"
    COMPLETED = "completed"
    CLOSED = "closed"

    @property
    def is_terminal(self) -> bool:
        return self in (WorkItemStatus.COMPLETED, WorkItemStatus.CLOSED)

    @property
    def client_label(self) -> str:
        """The coarser wording shown to clients. See CLIENT_STATUS_LABELS."""
        return CLIENT_STATUS_LABELS.get(self.value, self.label)

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


_STATUS_LABELS = {
    WorkItemStatus.OPEN: "Open",
    WorkItemStatus.CLAIMED: "Claimed",
    WorkItemStatus.IN_PROGRESS: "In Progress",
    WorkItemStatus.WAITING_CLIENT: "Waiting for Client",
    WorkItemStatus.WAITING_INTERNAL: "Waiting for Internal Team",
    WorkItemStatus.WAITING_THIRD_PARTY: "Waiting for Third Party",
    WorkItemStatus.ESCALATED: "Escalated",
    WorkItemStatus.COMPLETED: "Completed",
    WorkItemStatus.CLOSED: "Closed",
}


# What a client is shown, as opposed to what staff track.
#
# Deliberately coarser than the internal set. "Waiting for Third Party" and
# "Escalated" describe NexterPay's process rather than the client's situation,
# and telling a customer their case has been escalated invites a question
# nobody wants to answer. Agreed with NexterPay; their team may yet revise the
# wording, which is why it sits here as one mapping rather than scattered
# through the message text.
CLIENT_STATUS_LABELS = {
    "open": "Received",
    "claimed": "In progress",
    "in_progress": "In progress",
    "waiting_client": "Waiting on you",
    "waiting_internal": "In progress",
    "waiting_third_party": "In progress",
    "escalated": "In progress",
    "completed": "Resolved",
    "closed": "Resolved",
}


class Priority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class StaffRole(str, enum.Enum):
    """Section 13 of the PRD. Ordered least to most privileged."""

    OPERATOR = "operator"
    SENIOR_OPERATOR = "senior_operator"
    MANAGER = "manager"
    ADMINISTRATOR = "administrator"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: StaffRole) -> bool:
        return self.rank >= other.rank


_ROLE_RANK = {
    StaffRole.OPERATOR: 0,
    StaffRole.SENIOR_OPERATOR: 1,
    StaffRole.MANAGER: 2,
    StaffRole.ADMINISTRATOR: 3,
}


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"      # client -> NexterPay
    OUTBOUND = "outbound"    # NexterPay -> client
    INTERNAL = "internal"    # never leaves the Operations Group


class EventType(str, enum.Enum):
    """Every mutation of a work item emits exactly one of these.

    The event log is the audit trail. Nothing may change a work item without
    recording an event - see `app.domain.work_items`.
    """

    WORK_ITEM_CREATED = "work_item_created"
    TOPIC_CREATED = "topic_created"
    OWNERSHIP_CLAIMED = "ownership_claimed"
    OWNERSHIP_ASSIGNED = "ownership_assigned"
    OWNERSHIP_RELEASED = "ownership_released"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    INTERNAL_NOTE_ADDED = "internal_note_added"
    CLIENT_MESSAGE_RECEIVED = "client_message_received"
    STAFF_REPLY_SENT = "staff_reply_sent"
    ATTACHMENT_RECEIVED = "attachment_received"
    SUPPLIER_FILED = "supplier_filed"
    TICKETS_LINKED = "tickets_linked"
    TICKETS_UNLINKED = "tickets_unlinked"
    # One desk answering another. Distinct from an internal note, because it
    # is the thing the asking desk was waiting for, and from a staff reply,
    # because it never leaves NexterPay.
    INTERNAL_ANSWER_SENT = "internal_answer_sent"
    WORK_ITEM_CLOSED = "work_item_closed"
    WORK_ITEM_REOPENED = "work_item_reopened"
    TOPIC_CLOSED = "topic_closed"
