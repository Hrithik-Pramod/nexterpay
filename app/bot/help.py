"""What you can do, from where you are standing.

NexterPay's feedback on the documents was four points, and three of them are
really the same complaint: the guide is long, it only covers Support, and you
have to know which page you are on before it helps. Their own conclusion was
that it is fine to follow the bot's prompts instead.

So this is the code answer to a documentation problem. `/nphelp` reads the
group you are in and the role you hold in it, and lists what *you* can do
*here* - not everything the platform can do for somebody, somewhere.

That solves the three points properly rather than by writing more:

  * it is never Support-only, because it is generated from the department of
    the group it was sent in;
  * it never lists something you would be refused, because it is filtered by
    the role you hold on that desk;
  * and it does not need combining with anything, because it is one answer
    rather than three documents.

Kept out of the handlers so it can be tested as text, which is the only thing
about it that matters.
"""

from __future__ import annotations

from app.bot import commands as cmd
from app.domain.enums import ChatKind, Department, StaffRole


def _line(name: str, what: str) -> str:
    return f"/{name} — {what}"


def unregistered_group() -> str:
    return (
        "This group is not registered, so the bot does nothing here yet.\n\n"
        f"An administrator can set it up by sending /{cmd.SETUP} in this group."
    )


def for_client_group(department: Department, *, is_supplier: bool) -> str:
    """What a client or supplier can do. Deliberately short.

    Everything a counterparty needs is two ideas: start with /np, and reply to
    us to add to something. The rest is detail they should never need.
    """
    raise_label = (
        "Commercial Enquiry" if department is Department.BUSINESS else "Raise Request"
    )
    side = "supplier" if is_supplier else "client"
    return "\n".join([
        f"You are in a {side} group for {department.label}.",
        "",
        "The one thing to remember: start with /np.",
        "",
        _line(cmd.FRONT_DOOR, f"the menu. Tap {raise_label}, or My requests."),
        _line(f"{cmd.RAISE} <details>", "raise something in one go, without the menu"),
        _line(cmd.TICKETS, "everything open, plus anything resolved in the last "
                           "four weeks"),
        "",
        "To add to something you have already raised, reply to any message we "
        "have sent you about it. You do not need the reference.",
        "",
        "A message typed here without /np, and not as a reply, is an ordinary "
        "message in an ordinary group. If you want it tracked, start with /np.",
    ])


# Everything staff can do, with the role it needs. Ordered by how often it is
# reached for rather than alphabetically - somebody sending /nphelp is usually
# looking for one specific thing they have forgotten.
_STAFF: list[tuple[str, str, StaffRole]] = [
    (cmd.REPLY + " <message>", "send to the counterparty, immediately",
     StaffRole.OPERATOR),
    (cmd.NOTE + " <text>", "record an internal note - never leaves this group",
     StaffRole.OPERATOR),
    (cmd.HISTORY, "the full trail for this request", StaffRole.OPERATOR),
    (cmd.LINK + " <reference>", "tie this request to another one",
     StaffRole.OPERATOR),
    (cmd.UNLINK + " <reference>", "remove a link", StaffRole.OPERATOR),
    (cmd.WORKLOAD, "every open request on this desk, with owner and status",
     StaffRole.OPERATOR),
    (cmd.NEW_CLIENT, "open a request with a client", StaffRole.OPERATOR),
    (cmd.NEW_SUPPLIER, "open a request with a supplier", StaffRole.OPERATOR),
    (cmd.WHOAMI, "your departments, your role in each, and what it permits",
     StaffRole.OPERATOR),
    (cmd.ASSIGN, "hand a request to somebody - as a reply to them",
     StaffRole.SENIOR_OPERATOR),
    (cmd.BROADCAST, "one message to many counterparty groups",
     StaffRole.MANAGER),
]

_ADMIN: list[tuple[str, str]] = [
    (cmd.SETUP, "register a group, or add a person - as buttons"),
    (cmd.ADDUSER + " <role> <department>", "add somebody, as a reply to them"),
    (cmd.REMOVEUSER + " [department]", "take one desk off somebody, or all of them"),
    (cmd.SETCODE + " <CODE>", "a counterparty's four letters - in their group"),
    (cmd.ADDPARTY + " <CODE> <name>", "a counterparty with no Telegram group"),
    (cmd.SETLEAD, "name a contact in a counterparty group - as a reply to them"),
    (cmd.LEADS, "who is named for a group"),
    (cmd.REMOVELEAD, "unname somebody - as a reply to them"),
]


def for_operations_group(
    department: Department, role: StaffRole | None, *, is_administrator: bool = False
) -> str:
    """What this person can do on this desk.

    Filtered by role on purpose. Listing something they would be refused is
    how a help message becomes a source of confusion rather than an answer.
    """
    if role is None and not is_administrator:
        return (
            f"You are not registered for {department.label}, so nothing here "
            f"applies to you yet.\n\n"
            f"An administrator can add you by replying to one of your messages "
            f"with /{cmd.ADDUSER} operator {department.value}. You can belong "
            f"to more than one department."
        )

    effective = role or StaffRole.ADMINISTRATOR
    lines = [
        f"{department.label} Operations. You are "
        f"{effective.value.replace('_', ' ')} here.",
        "",
        "Most of the work is buttons, inside a request's topic: Claim, Reply "
        "to client and Close are on screen, and More opens the rest - Status, "
        "Priority, Note, History, File under supplier, Link ticket, and Ask "
        "another department.",
        "",
        "Commands you can use here:",
    ]
    lines += [
        "  " + _line(name, what)
        for name, what, needed in _STAFF
        if effective.at_least(needed)
    ]

    withheld = [
        (name, needed) for name, _, needed in _STAFF if not effective.at_least(needed)
    ]
    if withheld:
        # Named rather than hidden. "Why can I not broadcast" is a question
        # somebody will ask, and answering it here saves them asking.
        lines += [
            "",
            "Needs more seniority on this desk: "
            + ", ".join(
                f"/{name.split()[0]} ({needed.value.replace('_', ' ')})"
                for name, needed in withheld
            )
            + ".",
        ]

    if is_administrator:
        lines += ["", "Administration:"]
        lines += ["  " + _line(name, what) for name, what in _ADMIN]
        lines += [
            "",
            "Adding somebody and naming a contact both have to be sent as a "
            "reply to a message from that person. Telegram will not tell a bot "
            "who is in a group, so being pointed at something they wrote is the "
            "only way it can learn who they are.",
        ]

    return "\n".join(lines)


def build(chat, role: StaffRole | None, *, is_administrator: bool = False) -> str:
    """The whole answer, chosen by where the message came from."""
    if chat is None:
        return unregistered_group()
    if chat.kind is ChatKind.OPERATIONS:
        return for_operations_group(
            chat.department, role, is_administrator=is_administrator
        )
    return for_client_group(chat.department, is_supplier=bool(chat.is_supplier))
