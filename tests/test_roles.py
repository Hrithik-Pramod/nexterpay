"""`/nprole` — the permission ladder, in the group.

NexterPay asked for it on 5 September so the team can refer back to it rather
than hunt for a document. That is an easy request to satisfy badly: write the
list out, and watch it drift from the code the first time a threshold moves.
A reference document that has quietly stopped being true is worse than none,
because people act on it.

So the guards here are about truthfulness, not wording. Every threshold the
code enforces must appear in the reference, and the reference must be built
from those same constants rather than from a copy of them.
"""

from __future__ import annotations

import pytest

from app.bot import commands as cmd
from app.bot import roles
from app.domain.enums import StaffRole


def test_every_named_threshold_appears_in_the_reference() -> None:
    """The one that matters.

    If somebody adds ROLE_REQUIRED_TO_SOMETHING and forgets this file, the
    reference silently omits an action. Found by comparing against the module,
    so a new constant fails here rather than being discovered by a person who
    trusted the list.
    """
    from app.domain import work_items as wi

    thresholds = {
        name: getattr(wi, name)
        for name in dir(wi)
        if name.startswith("ROLE_REQUIRED_TO_")
    }
    assert thresholds, "no thresholds found - has the naming changed?"

    used = {needed for _, needed in roles.ACTIONS}
    missing = {
        name for name, role in thresholds.items() if role not in used
    }
    assert not missing, (
        f"these thresholds are enforced but absent from /{cmd.ROLE}: {missing}. "
        f"Add a line to roles.ACTIONS using the constant itself."
    )


def test_broadcast_is_listed_at_the_level_the_code_enforces() -> None:
    """It lives in another module, so it is the one most likely to drift."""
    from app.services.broadcast import ROLE_REQUIRED_TO_BROADCAST

    listed = [needed for what, needed in roles.ACTIONS if "Broadcast" in what]
    assert listed == [ROLE_REQUIRED_TO_BROADCAST]


@pytest.mark.parametrize(
    "action,expected",
    [
        ("Reassign", StaffRole.SENIOR_OPERATOR),
        ("Escalate", StaffRole.SENIOR_OPERATOR),
        ("Reopen", StaffRole.MANAGER),
        ("Broadcast", StaffRole.MANAGER),
        ("Set priority", StaffRole.OPERATOR),
        ("Close a request", StaffRole.OPERATOR),
    ],
)
def test_the_ladder_reads_as_the_code_behaves(action: str, expected) -> None:
    """Spot checks on the ones NexterPay have asked about by name, so the
    reference cannot quietly disagree with what people were told."""
    found = [needed for what, needed in roles.ACTIONS if what.startswith(action)]
    assert found == [expected], f"{action} is listed at {found}, not {expected}"


def test_every_administrator_command_named_is_real() -> None:
    """A reference that names a command nobody answers to sends people
    hunting. Checked against the command list itself."""
    import re

    text = " ".join(roles.ADMIN_ACTIONS)
    named = {n.lower() for n in re.findall(r"/([A-Za-z]{2,})", text)}
    unknown = named - set(cmd.ALL)
    assert not unknown, f"/{cmd.ROLE} names commands that do not exist: {unknown}"


def test_the_administrator_list_covers_the_admin_commands() -> None:
    """The other direction. An administrator command missing from the
    reference is one the team will not know they have."""
    import re

    text = " ".join(roles.ADMIN_ACTIONS)
    named = {n.lower() for n in re.findall(r"/([A-Za-z]{2,})", text)}

    should_appear = {
        cmd.SETUP, cmd.REGISTER_OPS, cmd.REGISTER_CLIENT, cmd.REGISTER_SUPPLIER,
        cmd.ADDUSER, cmd.REMOVEUSER, cmd.SETCODE, cmd.ADDPARTY,
        cmd.SETLEAD, cmd.LEADS, cmd.REMOVELEAD,
    }
    assert should_appear <= named, f"missing from the reference: {should_appear - named}"


def test_it_marks_the_readers_own_level() -> None:
    """The question behind "what can each level do" is usually "what can I
    do", and answering both at once costs one line."""
    from app.db.models import Staff, StaffDepartment
    from app.domain.enums import Department

    person = Staff(telegram_user_id=1, display_name="Gavs D", memberships=[])
    person.memberships = [
        StaffDepartment(department=Department.SUPPORT, role=StaffRole.MANAGER),
    ]

    text = roles.reference(person, Department.SUPPORT)
    marked = [line for line in text.splitlines() if "← you" in line]
    assert len(marked) == 1
    assert marked[0].startswith("Manager")


def test_it_says_nothing_about_you_when_it_does_not_know() -> None:
    assert "← you" not in roles.reference()


def test_both_rules_are_stated() -> None:
    """The two that produced real confusion in the group: seniority is per
    desk, administration is not."""
    text = roles.reference()
    assert "per desk" in text
    assert "any group" in text


def test_it_is_refused_outside_an_operations_group() -> None:
    """Same reasoning as /npwhoami. It describes how NexterPay is organised,
    and a counterparty has no business reading it in their own room."""
    import ast
    from pathlib import Path

    main = Path(__file__).resolve().parents[1] / "app" / "bot" / "main.py"
    tree = ast.parse(main.read_text(encoding="utf-8"))
    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "cmd_role"
    )
    source = ast.get_source_segment(main.read_text(encoding="utf-8"), handler)

    assert "ChatKind.OPERATIONS" in source
    assert source.index("ChatKind.OPERATIONS") < source.index("role_reference("), (
        "the reference is built before the group is checked"
    )
