"""The rate steps, and a guard that the route has no missing doors.

Twelve steps were specified, all twelve were built in `app.domain.fx`, and
555 tests passed. Then the flow was driven by hand in Telegram and stopped
dead at step 2:

    FXACME-1000 is rate requested, and that step needs it to be rate quoted
    or awaiting client confirmation.

The refusal was correct. `create_client_order` does require a quoted deal, and
nothing in the entire bot could quote one - `quote_client` and
`record_supplier_quote` had no command, no button, and no way in. Four of the
twelve steps and both rejection paths were unreachable code that every test
exercised directly.

That is the same fault as the missing Start FX button an hour earlier, and as
the broadcast bug in August, and the raising bug in September: the suite is
strong at the service layer and blind where a handler meets Telegram. So the
first test here is not about rates at all. It asks whether every step the
domain defines is reachable from a handler, and it is the test that would have
found this before a client did.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

from app.bot import commands as cmd
from app.bot.handlers import fx as handlers
from app.domain import fx
from app.domain.enums import FxOrderStatus, FxSide
from app.services import fx_relay

# --------------------------------------------------------------------------
# Helpers for reading the code rather than trusting it
# --------------------------------------------------------------------------

def _tree(module):
    return ast.parse(inspect.getsource(module))


def _named_calls(node) -> set[str]:
    return {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _domain_steps() -> set[str]:
    """Every public step in the domain, found by what it does.

    A step is an async function that writes an event, which is the definition
    the module itself uses: `record_event` is called by everything that moves a
    deal and by nothing that merely reads one. Derived rather than listed so
    that step thirteen, whenever NexterPay ask for it, is covered by this file
    the day it is written instead of the day somebody remembers to add it.
    """
    steps = set()
    for node in ast.walk(_tree(fx)):
        if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
            if "record_event" in _named_calls(node):
                steps.add(node.name)
    return steps


def _reached_from_handlers() -> set[str]:
    """Everything the handlers call as `fx.<something>`."""
    return {
        n.attr for n in ast.walk(_tree(handlers))
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "fx"
    }


def _required_states(function_name: str) -> set[str]:
    """The statuses a domain step will accept, read off its `_require_state`."""
    for node in ast.walk(_tree(fx)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_require_state"
                ):
                    return {a.attr for a in call.args if isinstance(a, ast.Attribute)}
    raise AssertionError(f"{function_name} has no _require_state call")


# --------------------------------------------------------------------------
# The guard that matters
# --------------------------------------------------------------------------

def test_every_step_of_the_route_has_a_telegram_entry_point() -> None:
    """No step may exist that a person cannot reach.

    This is the test that was missing. It fails for exactly the reason the
    live flow failed - a step built, tested and wired to nothing - and it does
    so without a database, a fixture or a running bot, because the fault was
    never in the behaviour of any single piece.
    """
    steps = _domain_steps()
    assert len(steps) >= 11, (
        f"only found {len(steps)} steps; the detection rule has probably "
        f"drifted from the code: {sorted(steps)}"
    )

    unreachable = sorted(steps - _reached_from_handlers())
    assert not unreachable, (
        "these steps exist in the domain and cannot be reached from Telegram, "
        f"which is how FXACME-1000 got stuck: {unreachable}"
    )


def test_the_rate_steps_in_particular() -> None:
    """Named outright, because these four were the ones that were missing.

    The test above would catch them, but it would catch them as a list of
    strings. This one says which four and why they matter, so that whoever
    reads the failure knows the deal cannot be priced rather than that
    something abstract is unwired.
    """
    reached = _reached_from_handlers()
    for step in (
        "record_supplier_quote",   # what the supplier quoted us
        "quote_client",            # what we quote the client
        "reject_supplier_rate",    # their price is no good
        "reject_client_rate",      # ours was turned down
        "record_hash",             # the settlement
    ):
        assert step in reached, f"{step} has no way in from Telegram"


def test_the_commands_exist_and_are_prefixed() -> None:
    for name in (cmd.QUOTE, cmd.HASH, cmd.REJECT):
        assert name in cmd.ALL, f"{name} is not in the command list"
        assert name.startswith(cmd.PREFIX), f"{name} would clash with another bot"
        # NexterPay asked the underscores out on 3 September.
        assert "_" not in name


def test_the_commands_are_registered_on_the_router() -> None:
    """In `cmd.ALL` proves it was named. This proves it answers."""
    from aiogram.filters import Command

    registered = set()
    for handler in handlers.router.message.handlers:
        for f in handler.filters or []:
            if isinstance(f.callback, Command):
                registered.update(str(c) for c in f.callback.commands)

    for name in (cmd.QUOTE, cmd.HASH, cmd.REJECT):
        assert name in registered, f"/{name} is defined but nothing answers it"


# --------------------------------------------------------------------------
# Every button reaches a handler - properly this time
# --------------------------------------------------------------------------

def _claimed_prefixes() -> set[str]:
    """The callback prefixes the fx router actually claims.

    Read from the decorators rather than assumed. The existing dead-button
    check in `test_wiring` asserts that fx callback data begins with `fx:`,
    which every string in this module does by construction - so it would have
    passed happily on a button whose verb had no handler at all. This reads the
    filters.
    """
    prefixes = set()
    for node in ast.walk(_tree(handlers)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            for sub in ast.walk(decorator):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "startswith"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                ):
                    prefixes.add(sub.args[0].value)
    return prefixes


class _Deal:
    id = 7
    display_reference = "FXACME-SPEX-1000"

    class status:
        label = "Rate quoted"


class _Item:
    id = 11
    display_reference = "SPEX-1043"
    subject = "rate for 250,000 EUR"


class _Supplier:
    name = "Pexi Exchange"


def _every_keyboard():
    return [
        handlers._pick_keyboard([_Deal()], "qdeal"),
        handlers._pick_keyboard([_Deal()], "hdeal"),
        handlers._pick_keyboard([_Deal()], "rdeal"),
        handlers._supplier_keyboard(7, [(_Item(), _Supplier())]),
        handlers._side_keyboard(7, [FxSide.SUPPLIER, FxSide.CLIENT]),
        handlers._action_keyboard("Save", "fx:qsave:7"),
        handlers._action_keyboard("Send", "fx:hsend:7"),
        handlers._action_keyboard("Record", f"fx:rsave:7:{FxSide.CLIENT.value}"),
        handlers._send_keyboard(7, FxSide.CLIENT),
        handlers._deal_keyboard([_Deal()], FxSide.SUPPLIER),
        handlers.confirm_keyboard(7, FxSide.CLIENT),
        handlers.receipt_keyboard(7),
    ]


def test_every_fx_button_reaches_a_handler_that_claims_it() -> None:
    claimed = _claimed_prefixes()
    assert claimed, "no callback prefixes found; the decorator scan has drifted"

    orphans = [
        button.callback_data
        for markup in _every_keyboard()
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
        and not any(button.callback_data.startswith(p) for p in claimed)
    ]
    assert not orphans, f"these buttons reach no handler: {sorted(set(orphans))}"


def test_the_guard_would_notice_a_verb_nobody_handles() -> None:
    """Proving the check above can fail.

    A guard that cannot be made to fail is decoration. `test_wiring`'s version
    of this check passes on the string below, which is why this file has its
    own.
    """
    claimed = _claimed_prefixes()
    invented = "fx:nosuchverb:7"
    assert invented.startswith("fx:")
    assert not any(invented.startswith(p) for p in claimed)


# --------------------------------------------------------------------------
# The pickers offer only what the domain will accept
# --------------------------------------------------------------------------

def test_the_quote_picker_offers_exactly_what_quoting_allows() -> None:
    """Otherwise the picker teaches the state machine by refusal.

    Both sides of this have moved during the build - the statuses
    `quote_client` accepts, and the ones `/npquote` lists - and a mismatch in
    either direction is a bug: too many and the desk is offered a deal that
    will be refused, too few and a deal that could be priced is invisible.
    """
    offered = {status.name for status in handlers.QUOTABLE}
    assert offered == _required_states("quote_client")


def test_the_settlement_picker_offers_exactly_what_settling_allows() -> None:
    assert _required_states("record_hash") == {"AWAITING_SETTLEMENT"}
    source = ast.unparse(_tree(handlers))
    assert "FxOrderStatus.AWAITING_SETTLEMENT" in source


def test_a_confirmed_deal_is_never_offered_for_re_pricing() -> None:
    """The figures are fixed once a client has agreed them."""
    for status in (
        FxOrderStatus.AWAITING_CLIENT_CONFIRMATION,
        FxOrderStatus.AWAITING_SUPPLIER_ACCEPTANCE,
        FxOrderStatus.AWAITING_SETTLEMENT,
        FxOrderStatus.AWAITING_RECEIPT,
        FxOrderStatus.CLOSED,
    ):
        assert status not in handlers.QUOTABLE


# --------------------------------------------------------------------------
# The margin check
# --------------------------------------------------------------------------

def test_an_ordinary_margin_passes_without_comment() -> None:
    assert handlers.check_margin(Decimal("1.1642"), Decimal("1.1700")) is None


def test_no_supplier_rate_means_nothing_to_compare() -> None:
    assert handlers.check_margin(None, Decimal("1.17")) is None


def test_a_misplaced_decimal_point_is_caught() -> None:
    """1.1642 typed as 11.642. Tenfold, and it looks like a good day."""
    warning = handlers.check_margin(Decimal("1.1642"), Decimal("11.642"))
    assert warning is not None
    assert "decimal point" in warning


def test_a_rate_below_the_supplier_is_called_what_it_is() -> None:
    warning = handlers.check_margin(Decimal("1.1642"), Decimal("1.1000"))
    assert warning is not None
    assert "lose money" in warning


def test_the_threshold_is_a_warning_not_a_refusal() -> None:
    """Ten per cent exactly is a wide margin, not a typo. NexterPay set the
    prices; this only says when one looks like a keystroke."""
    assert handlers.check_margin(Decimal("1.00"), Decimal("1.10")) is None
    assert handlers.check_margin(Decimal("1.00"), Decimal("1.11")) is not None


def test_the_warning_never_raises() -> None:
    """A zero or nonsense supplier rate must not take the flow down mid-deal."""
    assert handlers.check_margin(Decimal("0"), Decimal("1.16")) is None


# --------------------------------------------------------------------------
# The preview, which is the one screen showing both rates
# --------------------------------------------------------------------------

def test_the_preview_shows_both_rates_and_the_margin() -> None:
    text = handlers.quote_preview(
        "FXACME-SPEX-1000", "Pexi Exchange", Decimal("1.1642"), Decimal("1.1700")
    )
    assert "1.1642" in text
    assert "1.17" in text
    assert "0.0058" in text
    assert "Pexi Exchange" in text


def test_the_preview_says_nothing_has_been_sent() -> None:
    """The screen exists to be stopped at. It has to say so."""
    text = handlers.quote_preview(
        "FXACME-SPEX-1000", "Pexi", Decimal("1.16"), Decimal("1.17")
    )
    assert "not yet saved" in text
    assert "Nothing is sent" in text


def test_revising_our_own_price_shows_no_supplier_figure() -> None:
    """A figure this screen did not collect must not appear on it, or somebody
    will try to correct it here and find that they cannot."""
    text = handlers.quote_preview("FXACME-SPEX-1000", "Pexi", None, Decimal("1.17"))
    assert "1.17" in text
    assert "Margin" not in text


def test_the_margin_preview_is_unreachable_from_the_relay() -> None:
    """The one number that must never leave the Operations Group.

    `fx_relay` has exactly three functions that write to a counterparty and all
    three compose through `fx.view_for`, which reads one side's columns. This
    asserts the new preview did not quietly become a fourth route out.
    """
    assert "quote_preview" not in inspect.getsource(fx_relay)
    assert "check_margin" not in inspect.getsource(fx_relay)


# --------------------------------------------------------------------------
# Which rejection is on the table
# --------------------------------------------------------------------------

class _Order:
    def __init__(self, status, supplier_rate=None):
        self.status = status
        self.supplier_rate = supplier_rate


def test_a_supplier_rate_can_be_turned_down_while_we_are_still_shopping() -> None:
    order = _Order(FxOrderStatus.RATE_REQUESTED, Decimal("1.1642"))
    assert handlers.rejectable_sides(order) == [FxSide.SUPPLIER]


def test_there_is_nothing_to_reject_before_a_price_arrives() -> None:
    order = _Order(FxOrderStatus.RATE_REQUESTED, None)
    assert handlers.rejectable_sides(order) == []


def test_our_rate_can_be_turned_down_once_the_client_has_it() -> None:
    order = _Order(FxOrderStatus.RATE_QUOTED, Decimal("1.1642"))
    assert handlers.rejectable_sides(order) == [FxSide.CLIENT]


def test_a_settled_deal_has_no_rate_left_to_argue_about() -> None:
    for status in (
        FxOrderStatus.AWAITING_SETTLEMENT,
        FxOrderStatus.AWAITING_RECEIPT,
        FxOrderStatus.CLOSED,
    ):
        assert handlers.rejectable_sides(_Order(status, Decimal("1.16"))) == []


def test_every_offered_rejection_is_one_the_domain_accepts() -> None:
    """The whole point of `rejectable_sides`. A button that produces an error
    message is worse than no button."""
    supplier_ok = _required_states("reject_supplier_rate")
    client_ok = _required_states("reject_client_rate")

    for status in FxOrderStatus:
        order = _Order(status, Decimal("1.1642"))
        for side in handlers.rejectable_sides(order):
            allowed = supplier_ok if side is FxSide.SUPPLIER else client_ok
            assert status.name in allowed, (
                f"{side.value} rejection is offered on {status.name}, which the "
                f"domain refuses"
            )


def test_the_rejection_prompt_helper_always_speaks() -> None:
    """The price of an exemption in `test_silent_refusals`.

    `reject_pick_deal` ends a branch with a bare `return` after handing off to
    `_ask_reason`, and the silence guard cannot see inside a helper - so
    `_ask_reason` is listed in SPEAKING_HELPERS there. That listing is a claim
    about this function, and this is the check that makes it true: no branches,
    and it speaks. Add an `if` to `_ask_reason` and this fails, which is what
    stops the exemption from quietly becoming a hole.
    """
    for node in ast.walk(_tree(handlers)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ask_reason":
            branches = [n for n in ast.walk(node) if isinstance(n, ast.If)]
            assert not branches, (
                "_ask_reason has branched, so it can no longer be assumed to "
                "speak on every path - remove it from SPEAKING_HELPERS"
            )
            spoke = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"answer", "reply", "send_message"}
                for n in ast.walk(node)
            )
            assert spoke, "_ask_reason says nothing"
            return
    raise AssertionError("_ask_reason not found")


def test_the_two_prompts_ask_different_questions() -> None:
    supplier = handlers.reject_prompt(FxSide.SUPPLIER)
    client = handlers.reject_prompt(FxSide.CLIENT)
    assert supplier != client
    # A supplier who learns why, or what beat them, learns the market we buy in.
    assert "never the reason" in supplier
    assert "own words" in client


# --------------------------------------------------------------------------
# The hash, checked once
# --------------------------------------------------------------------------

def test_a_hash_is_checked_by_the_same_code_at_both_ends() -> None:
    """Catching a truncated paste on entry is only worth doing if the check
    agrees with the one that runs at the end. Two implementations that disagree
    would be worse than one that runs late."""
    for node in ast.walk(_tree(fx)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "record_hash":
            assert "check_hash" in _named_calls(node)
            break
    else:
        raise AssertionError("record_hash not found")

    source = ast.unparse(_tree(handlers))
    assert "fx.check_hash" in source, "the handler does not check on entry"


def test_half_a_hash_is_refused() -> None:
    with pytest.raises(fx.FxError):
        fx.check_hash("a1b2c3")
    with pytest.raises(fx.FxError):
        fx.check_hash("")
    with pytest.raises(fx.FxError):
        fx.check_hash("a1b2c3d4e5f6a7b8 c9d0")


def test_a_real_looking_hash_survives_the_paste() -> None:
    raw = "  6f1d8c0a3b5e7f2941a8c6d0b3e5f7a9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f1  "
    assert fx.check_hash(raw) == raw.strip()


# --------------------------------------------------------------------------
# The consistency check, which had the supplier's figures backwards
#
# Found by putting a real deal through Telegram, not by a test - every test it
# had fed it a client's figures, so the orientation it got wrong was the one
# nothing exercised. These are the actual numbers from that deal.
# --------------------------------------------------------------------------

def _figures(pays, pays_ccy, rate, receives, receives_ccy):
    return handlers.Figures(
        pays=Decimal(pays), pays_currency=pays_ccy,
        rate=Decimal(rate),
        receives=Decimal(receives), receives_currency=receives_ccy,
    )


def test_a_correct_supplier_order_does_not_warn() -> None:
    """The supplier sends 290,000 USDT and receives 250,000 EUR at 1.16.

    Correct, and it warned: "290,000 at 1.16 comes to about 336,400, not
    250,000". It would have done that on every supplier order ever entered.
    """
    assert handlers.check_consistent(
        _figures("290000", "USDT", "1.16", "250000", "EUR")
    ) is None


def test_a_correct_client_order_still_does_not_warn() -> None:
    """The other half of the same deal, which was always fine."""
    assert handlers.check_consistent(
        _figures("250000", "EUR", "1.1642", "291050", "USDT")
    ) is None


def test_a_tenfold_typo_fails_both_readings() -> None:
    """The check this exists for, and the thing accepting two orientations
    could have cost. It does not: a missing zero misses both."""
    warning = handlers.check_consistent(
        _figures("250000", "EUR", "1.1642", "29105", "USDT")
    )
    assert warning is not None
    assert "291,050" in warning


def test_the_second_orientation_is_doing_real_work() -> None:
    """Proving the fix is not vacuous.

    If `_agrees` were simply loose enough to pass everything, these tests would
    all pass while checking nothing. So: figures that only the reverse reading
    accepts must pass, and the forward reading alone must reject them.
    """
    supplier = _figures("290000", "USDT", "1.16", "250000", "EUR")
    forward_only = supplier.pays * supplier.rate
    assert not handlers._agrees(supplier.receives, forward_only), (
        "the supplier's figures agree forwards, so this test proves nothing"
    )
    assert handlers.check_consistent(supplier) is None


def test_the_tolerance_is_wide_enough_for_real_pricing() -> None:
    """Fees and spreads move the last digits. Refusing those blocks business."""
    assert handlers.check_consistent(
        _figures("250000", "EUR", "1.1642", "290500", "USDT")
    ) is None


def test_the_help_lists_the_route_for_finance_only() -> None:
    """A command nobody can find is not far off a command that does not exist,
    which is roughly what the last two hours were about."""
    from app.bot import help as help_text
    from app.domain.enums import Department, StaffRole

    finance = help_text.for_operations_group(Department.FINANCE, StaffRole.OPERATOR)
    for name in (cmd.QUOTE, cmd.ORDER_CLIENT, cmd.ORDER_SUPPLIER, cmd.HASH):
        assert f"/{name}" in finance

    support = help_text.for_operations_group(Department.SUPPORT, StaffRole.OPERATOR)
    assert f"/{cmd.QUOTE}" not in support
