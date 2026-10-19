"""A short, permanent name for a counterparty's group.

Jason, 19 October:

    There is a wider piece I'm thinking about where every client and supplier
    group has a Lead, its own short Group Code, and eventually associations
    with the services/routes they use... BBS Finance = #BBS... The thinking
    for later is around Broadcast. If Wave/XOF was down, we could potentially
    use those associations to identify which client groups are actually
    affected and only Broadcast to them, rather than sending something to
    everyone.

So this module is deliberately about **groups**, not about FX, and not about
counterparties. Three distinctions that look like hair-splitting until the
second use arrives:

**Not FX.** Nothing here mentions a deal, a supplier or a rate. FX is the
first caller because it is the one that needs it now. Routes and broadcast
targeting attach to the same group later without touching this file.

**Not the counterparty code.** `Client.code` is four letters, belongs to the
company, and is what every reference is built from - `FXACME-SPEX-1042`.
A counterparty can have several groups; BBS Finance and BBS Support are one
company and two rooms, and "which room was affected" is the question the
broadcast idea turns on. So this hangs off the chat.

In practice the two will often read the same - BBS Finance carrying `#BBS`
against a counterparty coded `BBSX` - and that is fine. They answer different
questions: the counterparty code says whose deal this is, the group code says
which room to speak in.

**Not a second place to be careful.** The codes written here are the ones
`shorthand.CODE_RE` has to find in a sentence, so the validation below is
derived from that pattern rather than written out again. A code that could be
set but never matched would be invisibly useless - the desk types `#BBSFIN`,
nothing resolves, and nothing explains why.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat
from app.domain.enums import ChatKind, Department
from app.domain.errors import DomainError
from app.domain.shorthand import CODE_BODY


class GroupCodeError(DomainError):
    """A code that cannot be set, or cannot be resolved."""


class BadGroupCode(GroupCodeError):
    """Not a shape the shorthand parser could ever find."""


class CodeTaken(GroupCodeError):
    """Another group already answers to this."""


# Exactly what `#...` matches in a sentence, anchored.
#
# Built from `shorthand.CODE_BODY` rather than retyped. If somebody widens the
# shorthand to five letters this widens with it; if somebody narrows it, codes
# that can no longer be found stop being settable on the same day. The
# alternative is two patterns that agree until they do not, and the failure
# then is silent: a code that is accepted, stored, and never matches anything.
SETTABLE = re.compile(rf"^{CODE_BODY}$")


def normalise(raw: str) -> str:
    """The code as it will be stored, or an explanation of why not.

    Upper-cased, because `#bbs` and `#BBS` are the same thing to anybody
    typing in a hurry and two rows would be a silent duplicate.
    """
    cleaned = (raw or "").strip().lstrip("#").upper()
    if not SETTABLE.match(cleaned):
        raise BadGroupCode(
            "A group code is two to four letters, no digits — BBS, ACME, WVE. "
            "That is what the desk can type as #BBS mid-sentence, so anything "
            "else could be saved and would then never be recognised."
        )
    return cleaned


async def holder_of(session: AsyncSession, code: str) -> Chat | None:
    """The group that answers to this code, anywhere on the platform.

    Deliberately not scoped to a department: uniqueness has to be global or
    `#BBS` means different things in two rooms, and the desk has no way to
    know which one they are getting.
    """
    result = await session.execute(
        select(Chat).where(Chat.group_code == code.upper())
    )
    return result.scalars().first()


async def assign(session: AsyncSession, chat: Chat, raw: str) -> str | None:
    """Give this group a code. Returns the one it had before, if any.

    Changing updates the row rather than adding another, which is what Jason
    asked for and is also the only shape that keeps the code unique.
    """
    code = normalise(raw)

    holder = await holder_of(session, code)
    if holder is not None and holder.id != chat.id:
        raise CodeTaken(
            f"{code} already belongs to {holder.title or 'another group'}. "
            f"Codes have to be unique or #{code} means two different rooms."
        )

    previous = chat.group_code
    chat.group_code = code
    await session.flush()
    return previous


async def resolve(
    session: AsyncSession,
    code: str,
    *,
    department: Department,
    suppliers: bool | None = None,
) -> Chat:
    """The group a `#CODE` means, for a desk standing in `department`.

    Raises rather than returning None, and the message says which of the four
    things went wrong. Jason: "If the code isn't recognised, or it can't
    confidently resolve the group, stop and tell Ops rather than guessing."

    Guessing here would put a client's figures in a supplier's group. There is
    no partial credit available: a code either names one group this desk may
    raise with, or the desk is told why not.
    """
    wanted = (code or "").strip().lstrip("#").upper()
    chat = await holder_of(session, wanted)

    if chat is None:
        raise GroupCodeError(
            f"Nothing is registered as #{wanted}. Set it in that group with "
            f"/npsetgroupcode, or leave the code off and pick from the list."
        )
    if chat.kind is not ChatKind.CLIENT or not chat.is_active:
        raise GroupCodeError(
            f"#{wanted} is not a client or supplier group this desk can raise "
            f"with."
        )
    if chat.department is not department:
        raise GroupCodeError(
            f"#{wanted} belongs to {chat.department.label}, and you are on "
            f"{department.label}. Raising across desks is not something to do "
            f"by typing a code."
        )
    if suppliers is not None and chat.is_supplier is not suppliers:
        is_a = "supplier" if chat.is_supplier else "client"
        wanted_a = "supplier" if suppliers else "client"
        raise GroupCodeError(
            f"#{wanted} is a {is_a} group and you asked for a {wanted_a}. "
            f"Nothing has been opened."
        )
    return chat


async def coded_groups(
    session: AsyncSession, department: Department
) -> list[Chat]:
    """Every group on this desk, coded or not, for `/npgroups`.

    The ones without a code are the point of the list - a desk reading it is
    usually asking "what can I type?" and the gaps are the answer.
    """
    result = await session.execute(
        select(Chat)
        .where(
            Chat.kind == ChatKind.CLIENT,
            Chat.department == department,
            Chat.is_active.is_(True),
        )
        .order_by(Chat.is_supplier, Chat.title)
    )
    return list(result.scalars().all())


__all__ = [
    "SETTABLE",
    "BadGroupCode",
    "CodeTaken",
    "GroupCodeError",
    "assign",
    "coded_groups",
    "holder_of",
    "normalise",
    "resolve",
]
