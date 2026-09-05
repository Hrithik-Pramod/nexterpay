"""What a counterparty must never be shown about how NexterPay is organised.

The leak tests elsewhere ask whether a client can see another client's
*request*. This one asks a narrower question that had been missed entirely:
whether a client can see NexterPay's *staff* - who covers which desk, and who
outranks whom.

Found on 4 September. Somebody ran /npwhoami in the Pexi supplier group and
the bot answered there, publishing his departments and his role on each into
a room the supplier is sitting in. Nothing in it is a secret exactly, and it
is still not theirs to read.
"""

from __future__ import annotations

import pytest

from app.bot import commands as cmd
from app.bot import main as bot_main
from app.domain.enums import ChatKind


def _person():
    from app.db.models import Staff, StaffDepartment
    from app.domain.enums import Department, StaffRole

    p = Staff(telegram_user_id=1, display_name="Gavs D", memberships=[])
    p.memberships = [
        StaffDepartment(department=Department.BUSINESS, role=StaffRole.OPERATOR),
        StaffDepartment(department=Department.COMPLIANCE, role=StaffRole.MANAGER),
    ]
    return p


class _Chat:
    def __init__(self, kind: ChatKind) -> None:
        self.kind = kind


# Every kind of place the command can be sent, and whether it may answer.
#
# Written as a table because the first version of this file tested by reading
# the handler's source for the right words, and passed against a build with
# the condition disabled - `if False and chat.kind is not ...` still contains
# every string it looked for. A test that inspects code instead of running it
# will accept anything shaped like a fix.
WHERE = [
    (ChatKind.OPERATIONS, True),
    (ChatKind.CLIENT, False),
]


@pytest.mark.parametrize("kind,may_answer", WHERE, ids=lambda v: str(v))
def test_whoami_answers_only_inside_an_operations_group(kind, may_answer) -> None:
    reply = bot_main.whoami_response(_Chat(kind), _person())
    revealing = ("Business" in reply or "manager" in reply.lower())

    if may_answer:
        assert revealing, "staff cannot check their own record any more"
    else:
        assert not revealing, (
            f"a {kind.value} group was shown NexterPay's staff structure:\n{reply}"
        )


def test_every_kind_of_chat_is_covered_by_that_table() -> None:
    """A sixth kind of group added later must not quietly default to
    answering. If ChatKind grows, this fails until somebody decides."""
    covered = {kind for kind, _ in WHERE}
    assert covered == set(ChatKind), f"not decided for: {set(ChatKind) - covered}"


def test_an_unregistered_group_is_refused_too() -> None:
    """The one we know least about is the one to be most careful in."""
    reply = bot_main.whoami_response(None, _person())
    assert "Business" not in reply and "manager" not in reply.lower()


def test_it_says_where_to_go_instead() -> None:
    """Refusing without redirecting is how a fix becomes the next bug report -
    the same lesson as the silent administrator commands."""
    reply = bot_main.whoami_response(_Chat(ChatKind.CLIENT), _person())
    assert "Operations Group" in reply
    assert f"/{cmd.HELP}" in reply


def test_somebody_unregistered_is_still_told_so() -> None:
    """In an Operations Group, "you are not staff" is a useful answer and
    must not have been swallowed by the new guard."""
    reply = bot_main.whoami_response(_Chat(ChatKind.OPERATIONS), None)
    assert "not registered" in reply.lower()


def test_the_answer_itself_names_departments_and_seniority() -> None:
    """Why any of the above matters. If whoami_text ever stopped naming
    these, the guard would be pointless ceremony - and if it started naming
    something worse, the guard is what stands between that and a supplier."""
    text = bot_main.whoami_text(_person())
    assert "Business" in text and "Compliance and Risk" in text
    assert "operator" in text and "manager" in text


def test_help_is_the_command_that_may_answer_anywhere() -> None:
    """The distinction being drawn.

    /nphelp is safe in a client group because it answers about the *group* -
    what can be done here - and is filtered to the client list when it is a
    client group. /npwhoami answers about the *person*, across every desk they
    hold, which is exactly what does not belong in that room.
    """
    from app.bot import help as help_module

    text = help_module.for_client_group(
        __import__("app.domain.enums", fromlist=["Department"]).Department.BUSINESS,
        is_supplier=True,
    )
    for word in ("operator", "senior", "manager", "administrator"):
        assert word not in text.lower(), (
            f"the client-facing help mentions {word!r}"
        )
