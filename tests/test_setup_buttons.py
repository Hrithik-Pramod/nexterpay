"""Administration by button, for the two jobs that need it.

NexterPay were asked which administrator actions were worth putting on
buttons, and named two: registering a group, and adding a person. Both are
done with somebody standing there waiting, and both are typed from memory -
"senior_operator" and the department spelling account for most of the failed
attempts so far.

Everything else stays a command. "All of them" was a much larger piece than it
sounded, and the rest are run once, calmly, with the reference open.
"""

from __future__ import annotations

import pytest

from app.bot import keyboards as kb
from app.domain.enums import Department, StaffRole


def test_the_menu_is_built_from_where_you_are_standing() -> None:
    """Offering "register this as an Operations Group" inside a client group
    is how a client group gets turned into an internal one by a mis-tap."""
    in_client = [
        b.text for row in kb.setup_menu(in_operations=False).inline_keyboard for b in row
    ]
    in_ops = [
        b.text for row in kb.setup_menu(in_operations=True).inline_keyboard for b in row
    ]

    assert any("client group" in t for t in in_client)
    assert any("supplier group" in t for t in in_client)
    assert not any("Add a person" in t for t in in_client)

    assert any("Add a person" in t for t in in_ops)
    assert not any("client group" in t for t in in_ops)


def test_every_department_is_offered_so_nobody_types_one() -> None:
    labels = [
        b.text for row in kb.department_menu("regdept").inline_keyboard for b in row
    ]
    for department in Department:
        assert department.label in labels, f"{department.label} is not offered"


def test_each_role_says_what_it_actually_permits() -> None:
    """"senior_operator" typed from memory is the commonest way this goes
    wrong, and the role name alone does not say what it buys."""
    labels = [
        b.text for row in kb.role_menu(Department.SUPPORT).inline_keyboard for b in row
    ]
    for role in StaffRole:
        assert any(role.value.split("_")[0].title() in t for t in labels)

    assert any("reassign" in t for t in labels)
    assert any("reopen" in t for t in labels)
    assert any("all departments" in t for t in labels)


def test_admin_callbacks_round_trip_including_the_compound_one() -> None:
    """setrole carries two values in one payload, which is the shape most
    likely to be parsed wrongly."""
    assert kb.parse_admin_cb(kb.admin_cb("cancel")) == ("cancel", None)
    assert kb.parse_admin_cb(kb.admin_cb("regdept", "finance")) == ("regdept", "finance")

    payload = kb.admin_cb("setrole", "compliance|senior_operator")
    assert len(payload.encode()) <= 64, "Telegram caps callback data at 64 bytes"
    action, value = kb.parse_admin_cb(payload)
    assert action == "setrole"
    assert value.split("|") == ["compliance", "senior_operator"]


def test_a_malformed_admin_callback_is_refused() -> None:
    with pytest.raises(ValueError):
        kb.parse_admin_cb("garbage")
    with pytest.raises(ValueError):
        kb.parse_admin_cb("wi:claim:1")


def test_the_two_keyboards_do_not_collide_with_the_work_item_ones() -> None:
    """Both live on the same dispatcher. A shared prefix would have one
    handler swallowing the other's taps."""
    assert kb.ADMIN_PREFIX != kb.PREFIX
    assert not kb.admin_cb("cancel").startswith(f"{kb.PREFIX}:")
    assert not kb.cb("close", 1).startswith(f"{kb.ADMIN_PREFIX}:")
