"""What you can do, from where you are standing.

NexterPay's feedback on the documents was four points and three were really
one complaint: the guide is long, it only covers Support, and you have to know
which page you are on before it helps. Their own conclusion was that following
the bot's prompts is fine.

So /nphelp is the code answer to a documentation problem, and these tests hold
it to that. It has to be right for every department rather than Support, and
it has to be filtered by what the person can actually do - a help message that
lists something you would be refused is worse than none.
"""

from __future__ import annotations

import pytest

from app.bot import commands as cmd
from app.bot import help as helptext
from app.domain.enums import ChatKind, Department, StaffRole


class _Chat:
    def __init__(self, kind, department, is_supplier=False):
        self.kind, self.department, self.is_supplier = kind, department, is_supplier


@pytest.mark.parametrize("department", list(Department))
def test_it_answers_for_every_department_not_just_support(department) -> None:
    """Gavin's exact complaint about the guide: "this only has information on
    support at moment, not the rest"."""
    ops = helptext.build(
        _Chat(ChatKind.OPERATIONS, department), StaffRole.OPERATOR
    )
    assert department.label in ops

    client = helptext.build(_Chat(ChatKind.CLIENT, department), None)
    assert department.label in client


def test_a_client_is_told_two_things_and_not_the_rest() -> None:
    """Everything a counterparty needs is: start with /np, and reply to add.
    Listing staff commands at them would be noise at best."""
    text = helptext.build(_Chat(ChatKind.CLIENT, Department.SUPPORT), None)

    assert f"/{cmd.FRONT_DOOR}" in text
    assert f"/{cmd.TICKETS}" in text
    assert "reply" in text.lower()

    for staff_only in (cmd.REPLY, cmd.NOTE, cmd.BROADCAST, cmd.WORKLOAD, cmd.ADDUSER):
        assert f"/{staff_only}" not in text, f"{staff_only} was offered to a client"


def test_a_business_client_is_told_the_right_button() -> None:
    business = helptext.build(_Chat(ChatKind.CLIENT, Department.BUSINESS), None)
    support = helptext.build(_Chat(ChatKind.CLIENT, Department.SUPPORT), None)

    assert "Commercial Enquiry" in business
    assert "Raise Request" in support


def test_a_supplier_is_told_it_is_a_supplier_group() -> None:
    text = helptext.build(
        _Chat(ChatKind.CLIENT, Department.FINANCE, is_supplier=True), None
    )
    assert "supplier group" in text


def test_an_operator_is_not_offered_what_they_would_be_refused() -> None:
    """A help message listing something you cannot do is a source of
    confusion rather than an answer."""
    text = helptext.build(
        _Chat(ChatKind.OPERATIONS, Department.SUPPORT), StaffRole.OPERATOR
    )
    offered, withheld = text.split("Needs more seniority")

    assert f"/{cmd.REPLY}" in offered
    assert f"/{cmd.HISTORY}" in offered

    # The half that matters, and the half this test originally missed.
    # Asserting only that broadcast appears in the withheld section passes
    # perfectly well if it ALSO appears in the offered one - which is exactly
    # what happens when somebody removes the role filter. So check both sides.
    assert f"/{cmd.BROADCAST}" not in offered, "an Operator was offered broadcasting"
    assert f"/{cmd.ASSIGN}" not in offered, "an Operator was offered reassignment"
    assert cmd.BROADCAST in withheld
    assert cmd.ASSIGN in withheld


def test_a_manager_is_offered_broadcasting() -> None:
    text = helptext.build(
        _Chat(ChatKind.OPERATIONS, Department.SUPPORT), StaffRole.MANAGER
    )
    before_withheld = text.split("Needs more seniority")[0]
    assert f"/{cmd.BROADCAST}" in before_withheld
    assert f"/{cmd.ASSIGN}" in before_withheld


def test_administration_is_shown_only_to_administrators() -> None:
    plain = helptext.build(
        _Chat(ChatKind.OPERATIONS, Department.SUPPORT), StaffRole.MANAGER
    )
    admin = helptext.build(
        _Chat(ChatKind.OPERATIONS, Department.SUPPORT),
        StaffRole.ADMINISTRATOR,
        is_administrator=True,
    )

    assert f"/{cmd.ADDUSER}" not in plain
    assert f"/{cmd.SETUP}" in admin
    assert f"/{cmd.SETLEAD}" in admin
    # And the thing people get wrong is explained rather than assumed.
    assert "reply" in admin.lower()
    assert "Telegram will not tell a bot who is in a group" in admin


def test_somebody_on_the_wrong_desk_is_told_how_to_be_added() -> None:
    text = helptext.build(_Chat(ChatKind.OPERATIONS, Department.FINANCE), None)

    assert "not registered for Finance" in text
    assert f"/{cmd.ADDUSER} operator finance" in text
    assert "more than one department" in text


def test_an_unregistered_group_says_so_and_says_what_to_do() -> None:
    text = helptext.build(None, None)
    assert "not registered" in text
    assert f"/{cmd.SETUP}" in text


def test_every_command_it_names_is_one_the_bot_answers_to() -> None:
    """The same check the documents get. A help message naming a command that
    does not exist is worse than the document doing it, because nobody thinks
    to doubt the bot."""
    import re

    known = set(cmd.ALL)
    texts = [
        helptext.build(None, None),
        helptext.build(_Chat(ChatKind.CLIENT, Department.SUPPORT), None),
        helptext.build(_Chat(ChatKind.OPERATIONS, Department.SUPPORT), None),
        *[
            helptext.build(_Chat(ChatKind.OPERATIONS, d), role, is_administrator=True)
            for d in Department
            for role in StaffRole
        ],
    ]
    for text in texts:
        for name in re.findall(r"(?<![\w.])/([a-zA-Z_]+)", text):
            assert name in known, f"/{name} is not a command the bot answers to"
