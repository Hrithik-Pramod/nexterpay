"""What each channel is for, in the client's own group.

NexterPay's feedback across Reports 3 and 4 kept circling the same thing: a
client raises a settlement question in Support, and it sits until somebody
notices it belongs to Finance. That is not a command problem — every command
worked — so no amount of guide-writing was going to fix it. The answer is one
sentence at the top of `/nphelp` saying what the room is for.

The wording is theirs, approved on 13 September, and quoted rather than
paraphrased. Compliance is the one they kept as drafted.

Also here: `/npraise` dropped from the client help, which they asked for on the
12th. The command still works — it is advertised less, not removed — and one of
these tests exists purely to keep those two facts apart, because "we took it
out of the help" and "we took it out" are very different messages to send a
client who is mid-sentence.
"""

from __future__ import annotations

from app.bot import commands as cmd
from app.bot import help as helptext
from app.domain.enums import Department

# --------------------------------------------------------------------------
# Every desk has one
# --------------------------------------------------------------------------

def test_every_department_has_a_purpose() -> None:
    """Structural, so a sixth desk cannot ship without wording.

    Compliance and Risk was added as a fifth department in September and a
    handful of places had to be found by hand afterwards. This is one of them,
    found in advance.
    """
    missing = [d.label for d in Department if d not in helptext.CHANNEL_PURPOSE]
    assert not missing, f"these desks have no channel description: {missing}"


def test_no_purpose_is_left_empty() -> None:
    for department, text in helptext.CHANNEL_PURPOSE.items():
        assert text.strip(), f"{department.label} has a blank description"
        assert text.strip().endswith("."), (
            f"{department.label} reads as a fragment; it is shown as a sentence"
        )


def test_the_wording_is_theirs() -> None:
    """Spot-checked on the phrase that is distinctly NexterPay's in each.

    Not the whole string, which would make this a copy of the source file and
    fail on a comma. These are the words that would be lost if somebody
    "tidied" the descriptions into house style.
    """
    for department, phrase in [
        (Department.SUPPORT, "day-to-day operational issues"),
        (Department.FINANCE, "settlement and reconciliation"),
        (Department.BUSINESS, "new corridors"),
        (Department.DEVELOPMENT, "API access"),
        (Department.COMPLIANCE, "KYC requests"),
    ]:
        assert phrase in helptext.CHANNEL_PURPOSE[department], (
            f"{department.label} no longer carries NexterPay's own wording"
        )


# --------------------------------------------------------------------------
# And it reaches the person in the room
# --------------------------------------------------------------------------

# Support's client side has its own message, written by NexterPay on
# 15 September. It replaces the whole help rather than a line of it, so it is
# outside the shared-purpose rules below and has its own tests further down.
SHARED = [d for d in Department if d is not Department.SUPPORT]


def test_the_purpose_leads_the_client_help() -> None:
    """Above the commands, because the mistake it prevents happens before
    anybody types anything."""
    for department in SHARED:
        text = helptext.for_client_group(department, is_supplier=False)
        purpose = helptext.CHANNEL_PURPOSE[department]
        assert purpose in text
        assert text.index(purpose) < text.index(f"/{cmd.FRONT_DOOR}"), (
            f"{department.label}: the commands come before the purpose"
        )


def test_a_supplier_group_says_the_same_thing() -> None:
    """NexterPay drew no distinction, and the room is the same room."""
    for department in Department:
        assert helptext.CHANNEL_PURPOSE[department] in helptext.for_client_group(
            department, is_supplier=True
        )


def test_each_desk_gets_its_own_and_not_another() -> None:
    """The failure that would make this feature worse than nothing: Finance
    being told what Support is for."""
    for department in SHARED:
        text = helptext.for_client_group(department, is_supplier=False)
        others = [
            other.label for other in Department
            if other is not department
            and helptext.CHANNEL_PURPOSE[other] in text
        ]
        assert not others, f"{department.label} also shows: {others}"


# --------------------------------------------------------------------------
# `/npraise` — quieter, not gone
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Support's client side, which NexterPay wrote themselves
# --------------------------------------------------------------------------

def test_support_clients_get_their_transaction_lookup() -> None:
    """The reason this message is longer than every other desk's.

    Support is where a client arrives holding a transaction reference with no
    idea what to do with it — which is the same problem the reference nudge
    solves from the other end.
    """
    text = helptext.for_client_group(Department.SUPPORT, is_supplier=False)
    assert "/orderstatus" in text
    assert "PayInExternalPending" in text
    assert "24 hours" in text


def test_support_suppliers_do_not_get_it() -> None:
    """A supplier does not look up a client's transaction. NexterPay were
    explicit: "only for client end display, yours remains for supplier end"."""
    text = helptext.for_client_group(Department.SUPPORT, is_supplier=True)
    assert "/orderstatus" not in text
    assert helptext.CHANNEL_PURPOSE[Department.SUPPORT] in text


def test_the_lookup_command_is_not_one_of_ours() -> None:
    """`/orderstatus` is answered on NexterPay's side.

    It has a named exception in test_help's guard. If this platform ever grows
    a command by that name it would be np-prefixed like every other, and the
    exception would then be hiding a real collision.
    """
    assert helptext.LOOKUP_COMMAND.lstrip("/") not in cmd.ALL


def test_every_client_help_offers_the_help_command() -> None:
    """NexterPay's own text lists it, so the others should not be the odd ones
    out — somebody reading two of these should not find the second one quieter
    about how to get back to it."""
    for department in Department:
        for supplier in (True, False):
            text = helptext.for_client_group(department, is_supplier=supplier)
            assert f"/{cmd.HELP}" in text, f"{department.label} supplier={supplier}"


def test_npraise_is_no_longer_advertised_to_clients() -> None:
    """NexterPay, 12 September: drop it from the help text.

    The menu is the route they want people on, and two ways of doing the same
    thing invites the question of which is right.
    """
    for department in Department:
        for supplier in (True, False):
            text = helptext.for_client_group(department, is_supplier=supplier)
            assert f"/{cmd.RAISE}" not in text, (
                f"{department.label} still offers /{cmd.RAISE} to counterparties"
            )


def test_npraise_still_works() -> None:
    """The distinction this file exists to hold.

    Dropping a command from the help is a change to the documentation. Dropping
    it from the bot is a change to the product, and it would strand every
    client who has it in muscle memory — with silence, which is this platform's
    worst failure.
    """
    from aiogram.filters import Command

    from app.bot.handlers import client

    registered = set()
    for handler in client.router.message.handlers:
        for f in handler.filters or []:
            if isinstance(f.callback, Command):
                registered.update(str(c) for c in f.callback.commands)

    assert cmd.RAISE in registered, (
        "/npraise was removed from the bot, not just from the help text"
    )
    assert cmd.RAISE in cmd.ALL
