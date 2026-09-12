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
    "claimed": "In Progress",
    "in_progress": "In Progress",
    "waiting_client": "Waiting on You",
    "waiting_internal": "In Progress",
    "waiting_third_party": "In Progress",
    "escalated": "In Progress",
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


class FxOrderStatus(str, enum.Enum):
    """Where an FX deal has got to.

    The test each of these had to pass, from the FX Flow note of 5 September:
    a stage earns its place if it changes who you are waiting on. Anyone
    looking at the board should be able to tell whose move it is without
    reading the conversation. Two consecutive stages that both mean "waiting
    on the client" are one state carrying two facts.

    Applied to NexterPay's description of 12 September, that gives seven and a
    return path. `AWAITING_SUPPLIER_ACCEPTANCE` and `AWAITING_SETTLEMENT` both
    wait on the supplier and both survive the test, because accepting an order
    and sending the money are different acts - a supplier can sit in the first
    for an hour and the second for five days, and the chase is different.
    """

    RATE_REQUESTED = "rate_requested"            # waiting on the supplier to quote us
    RATE_QUOTED = "rate_quoted"                  # waiting on the client
    RATE_REJECTED = "rate_rejected"              # waiting on us - the return path
    AWAITING_CLIENT_CONFIRMATION = "awaiting_client_confirmation"
    AWAITING_SUPPLIER_ACCEPTANCE = "awaiting_supplier_acceptance"
    AWAITING_SETTLEMENT = "awaiting_settlement"  # chasing happens in here
    AWAITING_RECEIPT = "awaiting_receipt"        # hash passed on, waiting on the client
    CLOSED = "closed"

    @property
    def label(self) -> str:
        return _FX_STATUS_LABELS[self]

    @property
    def waiting_on(self) -> str:
        """Whose move it is. The reason each state exists."""
        return _FX_WAITING_ON[self]

    @property
    def is_terminal(self) -> bool:
        return self is FxOrderStatus.CLOSED


_FX_STATUS_LABELS = {
    FxOrderStatus.RATE_REQUESTED: "Rate requested",
    FxOrderStatus.RATE_QUOTED: "Rate quoted",
    FxOrderStatus.RATE_REJECTED: "Rate rejected",
    FxOrderStatus.AWAITING_CLIENT_CONFIRMATION: "Awaiting client confirmation",
    FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE: "Awaiting supplier acceptance",
    FxOrderStatus.AWAITING_SETTLEMENT: "Awaiting settlement",
    FxOrderStatus.AWAITING_RECEIPT: "Awaiting receipt",
    FxOrderStatus.CLOSED: "Closed",
}

_FX_WAITING_ON = {
    FxOrderStatus.RATE_REQUESTED: "Supplier",
    FxOrderStatus.RATE_QUOTED: "Client",
    FxOrderStatus.RATE_REJECTED: "NexterPay",
    FxOrderStatus.AWAITING_CLIENT_CONFIRMATION: "Client",
    FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE: "Supplier",
    FxOrderStatus.AWAITING_SETTLEMENT: "Supplier",
    FxOrderStatus.AWAITING_RECEIPT: "Client",
    FxOrderStatus.CLOSED: "Nobody",
}


class FxSide(str, enum.Enum):
    """Which half of a deal an order belongs to.

    Load-bearing rather than descriptive. Every figure on an FX deal exists
    twice - our rate and the supplier's, what the client pays and what the
    supplier receives - and the difference is NexterPay's margin. A function
    that writes to a counterparty takes a side and reads only that side's
    fields, so a leak would need somebody to pass the wrong side rather than
    merely to forget which field was which.
    """

    CLIENT = "client"
    SUPPLIER = "supplier"


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

    # The FX deal. Separate event types rather than reusing STATUS_CHANGED,
    # because these carry money: "the rate was set to 1.1642 by Gavin" is the
    # line somebody will be reading back six weeks later when a client disputes
    # what was agreed, and a generic status change cannot hold it.
    FX_RATE_REQUESTED = "fx_rate_requested"
    FX_SUPPLIER_QUOTED = "fx_supplier_quoted"
    FX_SUPPLIER_RATE_REJECTED = "fx_supplier_rate_rejected"
    FX_RATE_QUOTED = "fx_rate_quoted"
    FX_RATE_REJECTED = "fx_rate_rejected"
    FX_ORDER_CREATED = "fx_order_created"
    FX_CLIENT_CONFIRMED = "fx_client_confirmed"
    FX_SUPPLIER_ACCEPTED = "fx_supplier_accepted"
    FX_HASH_RECORDED = "fx_hash_recorded"
    FX_RECEIPT_CONFIRMED = "fx_receipt_confirmed"
