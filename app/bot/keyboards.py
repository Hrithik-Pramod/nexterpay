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


def work_item_actions(
    work_item_id: int, *, claimed: bool, expanded: bool = False,
    asked_from: str | None = None,
) -> InlineKeyboardMarkup:
    """Three buttons and a More, rather than nine.

    NexterPay's observation, and it holds up: of nine actions, three carry
    almost all the traffic. Nine buttons on every request is nine things to
    read past to reach the one being reached for, every single time. The other
    six have not gone anywhere - they are one tap behind More, which is the
    right price for something used occasionally.

    Claim becomes Reassign once somebody owns it, so the first button is
    always the one about ownership.

    `asked_from` names the request this one was opened on behalf of, when
    another department was asked to look at something. Two things change.

    The middle button becomes Answer, because that is what this request is
    for - it exists to be answered, and its answer goes back to the desk that
    asked rather than out to the counterparty.

    And Reply to client is removed rather than sitting alongside. Finance
    writing directly to Acme about ACME-1038 would quote a reference Acme has
    never seen, about a question Acme never asked, from a desk they never
    contacted. The desk holding the client relationship talks to the client;
    the desk being asked talks back to them. One route out, and it is inward.
    """
    first = (
        InlineKeyboardButton(text="Reassign", callback_data=cb("reassign", work_item_id))
        if claimed
        else InlineKeyboardButton(text="Claim", callback_data=cb("claim", work_item_id))
    )

    middle = (
        InlineKeyboardButton(
            text=f"Answer {asked_from}"[:60], callback_data=cb("answer", work_item_id)
        )
        if asked_from
        else InlineKeyboardButton(
            text="✉ Reply to client", callback_data=cb("reply", work_item_id)
        )
    )

    rows = [
        [
            first,
            middle,
            InlineKeyboardButton(text="Close", callback_data=cb("close", work_item_id)),
        ]
    ]

    if not expanded:
        rows.append(
            [InlineKeyboardButton(text="More ⌄", callback_data=cb("more", work_item_id))]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    rows += [
        [
            InlineKeyboardButton(text="Status", callback_data=cb("status", work_item_id)),
            InlineKeyboardButton(text="Priority", callback_data=cb("priority", work_item_id)),
        ],
        [
            InlineKeyboardButton(text="Note", callback_data=cb("note", work_item_id)),
            InlineKeyboardButton(text="History", callback_data=cb("history", work_item_id)),
        ],
        [
            InlineKeyboardButton(
                text="File under supplier", callback_data=cb("file", work_item_id)
            ),
            InlineKeyboardButton(
                text="Link ticket", callback_data=cb("link", work_item_id)
            ),
        ],
        [
            InlineKeyboardButton(
                text="Ask another department", callback_data=cb("askdept", work_item_id)
            )
        ],
        [InlineKeyboardButton(text="Less ⌃", callback_data=cb("less", work_item_id))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_reply(work_item_id: int, lead=None) -> InlineKeyboardMarkup:
    """Last stop before a message leaves for a client group.

    The envelope and the client's name are on the button on purpose. Staff tap
    dozens of these a day and stop reading; the one thing that must never
    become muscle memory is sending internal wording to a customer.

    Where the group has a nominated lead, a second send button addresses them
    by name. Two buttons rather than one setting, because whether this
    particular message needs a specific person's attention is a decision per
    message, not a preference.
    """
    rows = [
        [
            InlineKeyboardButton(
                text="✉ Send to client", callback_data=cb("sendreply", work_item_id)
            ),
            InlineKeyboardButton(
                text="Cancel", callback_data=cb("cancelreply", work_item_id)
            ),
        ]
    ]
    if lead is not None:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text=f"✉ Send and tag {lead.display_name}"[:60],
                    callback_data=cb("sendreply", work_item_id, "tag"),
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def closed_actions(work_item_id: int) -> InlineKeyboardMarkup:
    """What is left once a request is closed.

    Everything else - claim, reply, status, priority, close - is refused by the
    domain on a closed item, so leaving those buttons on screen only invites
    taps that produce an error. History still makes sense: the record is the
    reason the topic is kept.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="History", callback_data=cb("history", work_item_id)
                ),
                InlineKeyboardButton(
                    text="Reopen", callback_data=cb("reopen", work_item_id)
                ),
            ]
        ]
    )


def supplier_choices(work_item_id: int, counterparties) -> InlineKeyboardMarkup:
    """Which supplier this request concerns.

    Only counterparties that have a code appear: the code is what the filing
    structure is built on, so one without a code cannot be filed against.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"{c.code} · {c.name}"[:60],
                callback_data=cb("setsupplier", work_item_id, str(c.id)),
            )
        ]
        for c in counterparties
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def link_choices(work_item_id: int, candidates, linked) -> InlineKeyboardMarkup:
    """Tickets this one can be tied to, and the ones it already is.

    Both lists are on the same keyboard on purpose. Someone opening this is
    asking "what is this connected to", and answering that with a list of
    things it is *not* connected to would be an odd way round. The existing
    links carry a cross, so the same screen adds and removes.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"✕ {i.display_reference} · {i.subject}"[:60],
                callback_data=cb("unlink", work_item_id, str(i.id)),
            )
        ]
        for i in linked
    ]
    rows += [
        [
            InlineKeyboardButton(
                text=f"{i.display_reference} · {i.subject}"[:60],
                callback_data=cb("dolink", work_item_id, str(i.id)),
            )
        ]
        for i in candidates
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def department_choices(work_item_id: int, departments) -> InlineKeyboardMarkup:
    """Which desk to ask. The one you are standing in is not offered."""
    rows = [
        [
            InlineKeyboardButton(
                text=d.label, callback_data=cb("setdept", work_item_id, d.value)
            )
        ]
        for d in departments
    ]
    rows.append([InlineKeyboardButton(text="← Back", callback_data=cb("back", work_item_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_internal(work_item_id: int, department) -> InlineKeyboardMarkup:
    """Nothing reaches a counterparty here, but it still opens a request on
    somebody else's desk - so it is confirmed like everything else."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Ask {department.label}"[:60],
                    callback_data=cb("sendinternal", work_item_id, department.value),
                ),
                InlineKeyboardButton(
                    text="Cancel", callback_data=cb("cancelinternal", work_item_id)
                ),
            ]
        ]
    )


def confirm_answer(work_item_id: int, to_reference: str) -> InlineKeyboardMarkup:
    """Previewed like every other outbound message, even though this one only
    travels between two Operations Groups.

    It is still somebody else's ticket, and the answer is the thing they have
    been waiting on. A half-typed sentence landing there as the answer is a
    smaller mess than one reaching a client, and it is still a mess.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Send to {to_reference}"[:60],
                    callback_data=cb("sendanswer", work_item_id),
                ),
                InlineKeyboardButton(
                    text="Cancel", callback_data=cb("cancelanswer", work_item_id)
                ),
            ]
        ]
    )


def assignee_choices(work_item_id: int, people, department=None) -> InlineKeyboardMarkup:
    """Who to hand the request to, as buttons.

    The alternative was `/assign <telegram id>`, which nobody knows, or
    replying to a message from the person - which only works if they happen
    to have posted in that topic already. Neither is usable on a fresh
    request, which is precisely when reassignment happens.
    """
    def label(person) -> str:
        # Their seniority on this desk. Someone who is a Manager in Support
        # and an Operator in Compliance should read as an Operator here, or
        # the list would misrepresent what they can actually do with it.
        role = person.role_in(department) if department is not None else None
        if role is None and len(person.memberships) == 1:
            role = person.memberships[0].role
        suffix = f" · {role.value.replace('_', ' ')}" if role else ""
        return f"{person.display_name}{suffix}"

    rows = [
        [
            InlineKeyboardButton(
                text=label(p),
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


def open_requests(items) -> InlineKeyboardMarkup:
    """One button per open request. Tapping it posts a fresh anchor to reply to."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{i.client_reference} · {i.subject}"[:60],
                    callback_data=f"tk:open:{i.id}",
                )
            ]
            for i in items
        ]
    )


def acknowledgement_actions() -> InlineKeyboardMarkup:
    """Sits under every acknowledgement, so the list is always one tap away.

    Says "My requests", the same as the menu. It said "My open requests" while
    the menu said "My requests" - one action, two names - and "open" was
    wrong besides, because the list carries four weeks of resolved ones too.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="My requests", callback_data="tk:list")]
        ]
    )


def raise_request_prompt(department: str) -> InlineKeyboardMarkup:
    """What a client sees at the front door.

    Two buttons rather than one. NexterPay's point: somebody sending /np is
    as likely to be chasing something they already raised as starting
    something new, and offering only "Raise Request" makes checking require
    knowing a second command exists. It also quietly encourages a duplicate,
    which is the thing they then have to close by hand.

    Business groups get 'Commercial Enquiry' per PRD 15.4; everyone else gets
    'Raise Request'.
    """
    label = "Commercial Enquiry" if department == "business" else "Raise Request"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="raise:new")],
            [InlineKeyboardButton(text="My requests", callback_data="tk:list")],
        ]
    )


# --------------------------------------------------------------------------
# Administration by button
#
# Only two of the administrator commands are here. NexterPay were asked which
# were worth it and named these: registering a group and adding staff are the
# ones done under time pressure with somebody standing there waiting, and they
# are the two where getting the arguments wrong is most annoying. The rest are
# run once, calmly, and stay as commands.
# --------------------------------------------------------------------------

ADMIN_PREFIX = "ad"


def admin_cb(action: str, value: str | None = None) -> str:
    return f"{ADMIN_PREFIX}:{action}" + (f":{value}" if value else "")


def parse_admin_cb(data: str) -> tuple[str, str | None]:
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != ADMIN_PREFIX:
        raise ValueError(f"Unrecognised admin callback: {data!r}")
    return parts[1], parts[2] if len(parts) > 2 else None


def setup_menu(*, in_operations: bool, registered: bool) -> InlineKeyboardMarkup:
    """What can be set up from here, given what this group already is.

    Three states, not two, and getting that wrong is what NexterPay reported
    on 4 September: "this only offers option of person not group". They were
    right, and the gap was wider than it looked - registering an Operations
    Group was not on the menu anywhere, in any group, so the one command the
    buttons exist to replace was the one command you still had to type.

    An *unregistered* group is asked what it is, and all three answers are
    offered. Registering an Operations Group is a different question from
    registering a client group and the wrong answer is expensive, so it is
    worded as what the group is rather than as an action.

    A *registered* group is not offered registration again. Not because it
    would fail - it would succeed, and re-pointing a live client group at
    another department by mis-tap is a worse outcome than having to type the
    command deliberately.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if not registered:
        rows += [
            [InlineKeyboardButton(
                text="Our own Operations Group", callback_data=admin_cb("regops"))],
            [InlineKeyboardButton(
                text="A client's group", callback_data=admin_cb("regclient"))],
            [InlineKeyboardButton(
                text="A supplier's group", callback_data=admin_cb("regsupplier"))],
        ]
    elif in_operations:
        rows.append(
            [InlineKeyboardButton(text="Add a person", callback_data=admin_cb("adduser"))]
        )
    else:
        # A registered counterparty group. Naming a contact is the thing
        # actually done in here, and it was reachable only as a command.
        rows.append(
            [InlineKeyboardButton(
                text="Name a contact here", callback_data=admin_cb("howlead"))]
        )

    rows.append([InlineKeyboardButton(text="Cancel", callback_data=admin_cb("cancel"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def department_menu(action: str) -> InlineKeyboardMarkup:
    """Every department, as buttons, so nobody has to remember the spelling."""
    from app.domain.enums import Department

    rows = [
        [
            InlineKeyboardButton(
                text=d.label, callback_data=admin_cb(action, d.value)
            )
        ]
        for d in Department
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=admin_cb("cancel"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_menu(department) -> InlineKeyboardMarkup:
    """Roles, with what each one means, because "senior_operator" typed from
    memory is the single most common way adding somebody goes wrong."""
    from app.domain.enums import StaffRole

    labels = {
        StaffRole.OPERATOR: "Operator — work requests",
        StaffRole.SENIOR_OPERATOR: "Senior Operator — also reassign, escalate",
        StaffRole.MANAGER: "Manager — also reopen, broadcast",
        StaffRole.ADMINISTRATOR: "Administrator — everything, all departments",
    }
    rows = [
        [
            InlineKeyboardButton(
                text=labels[role],
                callback_data=admin_cb("setrole", f"{department.value}|{role.value}"),
            )
        ]
        for role in StaffRole
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=admin_cb("cancel"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
