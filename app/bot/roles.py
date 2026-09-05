"""What each level may do, generated from the checks themselves.

NexterPay asked for this as `/nprole` on 5 September, so the team can refer to
it in the group rather than hunting for a document. That request is easy to
satisfy badly: write the list out, and watch it drift from the code the first
time a threshold moves.

So nothing here is typed twice. Every line is built from the same constant the
permission check uses - change `ROLE_REQUIRED_TO_REOPEN` and this changes with
it. `tests/test_roles.py` fails if a new threshold appears that nothing here
mentions, which is the only way a reference document stays true.

The administrator entries are the exception: those commands are gated by
`_is_admin` rather than by a named constant, so they are listed by name and
the test checks the list against `commands.ALL`.
"""

from __future__ import annotations

from app.bot import commands as cmd
from app.domain.enums import StaffRole
from app.domain.work_items import (
    ROLE_REQUIRED_TO_CHANGE_PRIORITY,
    ROLE_REQUIRED_TO_ESCALATE,
    ROLE_REQUIRED_TO_REASSIGN,
    ROLE_REQUIRED_TO_REOPEN,
)
from app.services.broadcast import ROLE_REQUIRED_TO_BROADCAST

# Every action a level gates, against the constant that gates it.
#
# Ordered as somebody reads it: the everyday work first, then what each step
# up adds. Actions with no named constant are marked OPERATOR because that is
# what `require_any` resolves to - the lowest level, meaning any registered
# member of staff on that desk.
ACTIONS: list[tuple[str, StaffRole]] = [
    ("Claim a request", StaffRole.OPERATOR),
    ("Reply to the counterparty", StaffRole.OPERATOR),
    ("Add an internal note", StaffRole.OPERATOR),
    ("Set status — In Progress, Waiting, Completed", StaffRole.OPERATOR),
    ("Set priority, up to Critical", ROLE_REQUIRED_TO_CHANGE_PRIORITY),
    ("Close a request", StaffRole.OPERATOR),
    ("File a request under a supplier", StaffRole.OPERATOR),
    ("Link and unlink related requests", StaffRole.OPERATOR),
    ("Ask another department, and answer one", StaffRole.OPERATOR),
    (f"Raise outbound — /{cmd.NEW_CLIENT}, /{cmd.NEW_SUPPLIER}", StaffRole.OPERATOR),
    (f"See the desk's workload and any history — /{cmd.WORKLOAD}", StaffRole.OPERATOR),

    ("Reassign a request to somebody else", ROLE_REQUIRED_TO_REASSIGN),
    ("Escalate — set the status to Escalated", ROLE_REQUIRED_TO_ESCALATE),

    ("Reopen a closed request", ROLE_REQUIRED_TO_REOPEN),
    (f"Broadcast to counterparty groups — /{cmd.BROADCAST}", ROLE_REQUIRED_TO_BROADCAST),
]

# The administrator commands. Gated by `_is_admin` rather than by a level
# constant, because administration is not tied to a desk - see the note at the
# end of the reply.
ADMIN_ACTIONS: list[str] = [
    f"Register a group — /{cmd.SETUP}, or /{cmd.REGISTER_OPS}, "
    f"/{cmd.REGISTER_CLIENT}, /{cmd.REGISTER_SUPPLIER}",
    f"Add and remove staff, and set their level — /{cmd.ADDUSER}, /{cmd.REMOVEUSER}",
    f"Set a counterparty's four-letter code — /{cmd.SETCODE}",
    f"Add a counterparty with no group — /{cmd.ADDPARTY}",
    f"Name and remove contacts — /{cmd.SETLEAD}, /{cmd.LEADS}, /{cmd.REMOVELEAD}",
]

_ORDER = [
    StaffRole.OPERATOR,
    StaffRole.SENIOR_OPERATOR,
    StaffRole.MANAGER,
    StaffRole.ADMINISTRATOR,
]

_INTRO = {
    StaffRole.OPERATOR: "the working level. Everything day to day.",
    StaffRole.SENIOR_OPERATOR: "adds",
    StaffRole.MANAGER: "adds",
    StaffRole.ADMINISTRATOR: "adds the setup and the people.",
}


def reference(person=None, department=None) -> str:
    """The whole ladder, with the reader's own level marked where we know it."""
    mine = person.role_in(department) if person is not None and department else None

    lines = [
        "What each level can do. Each one includes everything below it.",
        "",
    ]
    for role in _ORDER:
        label = role.value.replace("_", " ").title()
        here = "     ← you, on this desk" if mine is role else ""
        lines.append(f"{label} — {_INTRO[role]}{here}")

        if role is StaffRole.ADMINISTRATOR:
            lines += [f"  · {what}" for what in ADMIN_ACTIONS]
        else:
            lines += [f"  · {what}" for what, needed in ACTIONS if needed is role]
        lines.append("")

    lines += [
        "Two rules worth knowing.",
        "",
        "Seniority is held per desk. Being a Manager in Support does not make "
        "you a Manager in Finance. If you work two desks you can hold a "
        f"different level on each — /{cmd.WHOAMI} shows yours.",
        "",
        "Administration is not. Registering groups, managing staff and setting "
        "codes work in any group, whichever desks that person is on. Setting "
        "up a department you do not work on is what it is for.",
    ]
    return "\n".join(lines)
