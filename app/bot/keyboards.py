"""Inline keyboards and their callback payloads.

Callback data is `wi:<action>:<work_item_id>[:<value>]`. Telegram caps callback
data at 64 bytes, which is ample here.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.enums import Priority, WorkItemStatus

PREFIX = "wi"

# Statuses staff set directly from the topic. Claimed is implicit in claiming,
# Closed goes through the Close button, so neither appears here.
QUICK_STATUSES = [
    WorkItemStatus.IN_PROGRESS,
    WorkItemStatus.WAITING_CLIENT,
    WorkItemStatus.WAITING_INTERNAL,
    WorkItemStatus.WAITING_THIRD_PARTY,
    WorkItemStatus.ESCALATED,
    WorkItemStatus.COMPLETED,
]


def cb(action: str, work_item_id: int, value: str | None = None) -> str:
    return f"{PREFIX}:{action}:{work_item_id}" + (f":{value}" if value else "")


def parse_cb(data: str) -> tuple[str, int, str | None]:
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != PREFIX:
        raise ValueError(f"Unrecognised callback data: {data!r}")
    return parts[1], int(parts[2]), parts[3] if len(parts) > 3 else None


def work_item_actions(work_item_id: int, *, claimed: bool) -> InlineKeyboardMarkup:
    first = (
        InlineKeyboardButton(text="Reassign", callback_data=cb("reassign", work_item_id))
        if claimed
        else InlineKeyboardButton(text="Claim", callback_data=cb("claim", work_item_id))
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                first,
                InlineKeyboardButton(text="Status", callback_data=cb("status", work_item_id)),
            ],
            [
                InlineKeyboardButton(
                    text="✉ Reply to client", callback_data=cb("reply", work_item_id)
                ),
                InlineKeyboardButton(text="Note", callback_data=cb("note", work_item_id)),
            ],
            [
                InlineKeyboardButton(text="Priority", callback_data=cb("priority", work_item_id)),
                InlineKeyboardButton(text="History", callback_data=cb("history", work_item_id)),
            ],
            [InlineKeyboardButton(text="Close", callback_data=cb("close", work_item_id))],
        ]
    )


def confirm_reply(work_item_id: int) -> InlineKeyboardMarkup:
    """Last stop before a message leaves for a client group.

    The envelope and the client's name are on the button on purpose. Staff tap
    dozens of these a day and stop reading; the one thing that must never
    become muscle memory is sending internal wording to a customer.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉ Send to client", callback_data=cb("sendreply", work_item_id)
                ),
                InlineKeyboardButton(
                    text="Cancel", callback_data=cb("cancelreply", work_item_id)
                ),
            ]
        ]
    )


def closed_actions(work_item_id: int) -> InlineKeyboardMarkup:
    """What is left once a request is closed.

    Everything else - claim, reply, status, priority, close - is refused by the
    domain on a closed item, so leaving those buttons on screen only invites
    taps that produce an error. History still makes sense: the record is the
    reason the topic is kept.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="History", callback_data=cb("history", work_item_id))]
        ]
    )


def assignee_choices(work_item_id: int, people) -> InlineKeyboardMarkup:
    """Who to hand the request to, as buttons.

    The alternative was `/assign <telegram id>`, which nobody knows, or
    replying to a message from the person - which only works if they happen
    to have posted in that topic already. Neither is usable on a fresh
    request, which is precisely when reassignment happens.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.display_name} · {p.role.value.replace('_', ' ')}",
                callback_data=cb("setowner", work_item_id, str(p.id)),
            )
        ]
        for p in people
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_choices(work_item_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s.label, callback_data=cb("setstatus", work_item_id, s.value))]
        for s in QUICK_STATUSES
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def priority_choices(work_item_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=p.label, callback_data=cb("setpriority", work_item_id, p.value)
            )
        ]
        for p in Priority
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def raise_request_prompt(department: str) -> InlineKeyboardMarkup:
    """The single button a client sees in their group.

    Business groups get 'Commercial Enquiry' per PRD 15.4; everyone else gets
    'Raise Request'.
    """
    label = "Commercial Enquiry" if department == "business" else "Raise Request"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="raise:new")]]
    )
