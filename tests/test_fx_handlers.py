"""The FX commands, where the handlers meet Telegram.

This is the layer that has produced every bug in this project that 500 passing
tests did not see: the broadcast one in August, raising in September, and
attachments last week. In all three the service layer was correct and the
handler never called it properly.

So the decisions a handler makes are pulled out of the handler - `parse_pair`,
`check_consistent`, `default_account_name`, `preview_text`, `confirm_keyboard`
- and tested directly, with structural guards on the wiring that cannot be
pulled out.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

from app.bot import commands as cmd
from app.bot.handlers import fx as fx_handlers
from app.db.models import Client
from app.domain import fx
from app.domain.enums import FxSide


# --------------------------------------------------------------------------
# Reading what somebody typed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "typed,amount,currency",
    [
        ("250000 EUR", Decimal("250000"), "EUR"),
        ("250,000 eur", Decimal("250000"), "EUR"),
        ("  1250000.50   usdt  ", Decimal("1250000.50"), "USDT"),
    ],
)
def test_an_amount_and_its_currency_are_read_together(typed, amount, currency) -> None:
    assert fx_handlers.parse_pair(typed) == (amount, currency)


@pytest.mark.parametrize("typed", ["250000", "EUR", "", "250000 123", "250000 E"])
def test_an_amount_without_a_currency_is_refused(typed) -> None:
    """An amount without a currency is not an amount, and this figure goes to a
    counterparty as a commitment."""
    with pytest.raises(fx.FxError):
        fx_handlers.parse_pair(typed)


# --------------------------------------------------------------------------
# Do the numbers agree
# --------------------------------------------------------------------------

def _figures(pays, rate, receives) -> fx_handlers.Figures:
    return fx_handlers.Figures(
        pays=Decimal(pays), pays_currency="EUR",
        rate=Decimal(rate),
        receives=Decimal(receives), receives_currency="USDT",
    )


def test_figures_that_agree_raise_no_warning() -> None:
    assert fx_handlers.check_consistent(_figures("250000", "1.1642", "291050")) is None


def test_small_drift_is_allowed() -> None:
    """Real rates carry fees, rounding and spreads. Refusing a deal because the
    arithmetic is off by a little would block real business."""
    assert fx_handlers.check_consistent(_figures("250000", "1.1642", "290500")) is None


def test_an_order_out_by_a_factor_of_ten_is_flagged() -> None:
    """The typo worth catching, and the person typing is the last one who can
    catch it cheaply."""
    warning = fx_handlers.check_consistent(_figures("250000", "1.1642", "29105"))
    assert warning is not None
    assert "291,050" in warning


def test_the_warning_does_not_block() -> None:
    """Returns a string rather than raising. An operator who knows the deal is
    right must be able to carry on."""
    result = fx_handlers.check_consistent(_figures("250000", "1.1642", "29105"))
    assert isinstance(result, str)


# --------------------------------------------------------------------------
# The name on the order, which is what keeps the sides apart
# --------------------------------------------------------------------------

def test_a_client_order_is_named_for_the_client() -> None:
    client = Client(name="Acme Payments Ltd", code="ACME")
    supplier = Client(name="Supplier Pexi", code="SPEX")
    assert fx_handlers.default_account_name(FxSide.CLIENT, client, supplier) == (
        "Acme Payments Ltd"
    )


def test_a_supplier_order_is_never_named_for_the_client() -> None:
    """NexterPay, 12 September: "for supplier, we have coding Nexterpay7".
    The supplier is quoting into an account, not to a named customer."""
    client = Client(name="Acme Payments Ltd", code="ACME")
    supplier = Client(name="Supplier Pexi", code="SPEX")
    suggested = fx_handlers.default_account_name(FxSide.SUPPLIER, client, supplier)

    assert "Acme" not in suggested
    assert suggested.startswith("Nexterpay")


# --------------------------------------------------------------------------
# The preview, which exists to stop the wrong figures going to the wrong side
# --------------------------------------------------------------------------

def test_the_preview_names_the_side_and_the_counterparty() -> None:
    text = fx_handlers.preview_text(
        FxSide.SUPPLIER, "Supplier Pexi",
        _figures("285200", "1.1408", "250000"), "Nexterpay7",
    )
    assert "Supplier Pexi" in text
    assert "supplier" in text
    assert "Nothing has been sent yet." in text


def test_the_preview_shows_every_figure_that_is_about_to_leave() -> None:
    """A confirmation screen that hides part of what it is confirming is the
    same class of fault as sending the wrong thing - see the attachment bug."""
    text = fx_handlers.preview_text(
        FxSide.CLIENT, "Acme Payments",
        _figures("250000", "1.1642", "291050"), "Acme Payments Ltd",
    )
    assert "1.1642" in text
    assert "250,000" in text
    assert "291,050" in text
    assert "Acme Payments Ltd" in text


# --------------------------------------------------------------------------
# The buttons the counterparty taps
# --------------------------------------------------------------------------

def test_the_confirm_button_carries_the_side_it_belongs_to() -> None:
    """Without it, a confirmation cannot be told from the other side's."""
    markup = fx_handlers.confirm_keyboard(42, FxSide.SUPPLIER)
    data = markup.inline_keyboard[0][0].callback_data
    assert data == "fx:confirm:42:supplier"


def test_there_is_no_reject_button() -> None:
    """A counterparty who does not accept has something to say about why, and
    that is a conversation. Rejection is recorded by the desk from what they
    actually said, so it keeps their words."""
    markup = fx_handlers.confirm_keyboard(42, FxSide.CLIENT)
    labels = [b.text.lower() for row in markup.inline_keyboard for b in row]
    assert not any("reject" in label or "decline" in label for label in labels)
    assert len(labels) == 1


# --------------------------------------------------------------------------
# Wiring - the part that cannot be pulled out of the handler
# --------------------------------------------------------------------------

def _fn(name: str):
    tree = ast.parse(inspect.getsource(fx_handlers))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_sending_an_order_writes_the_figures_before_it_sends_them() -> None:
    """The order has to exist in the database before it reaches a counterparty.
    Reversed, a confirmation could arrive for a deal with no figures on it."""
    source = ast.unparse(_fn("send_order"))
    created = source.index("create")
    sent = source.index("fx_relay.send_order")
    assert created < sent, "the order is sent before it is recorded"


def test_sending_passes_the_side_through_to_the_relay() -> None:
    """The whole separation rests on the relay being told which side it is
    composing for. A handler that drops it would compose the wrong half."""
    source = ast.unparse(_fn("send_order"))
    assert "fx_relay.send_order" in source
    assert "side" in source


def test_the_client_and_supplier_creators_are_not_interchangeable() -> None:
    """`create_client_order` and `create_supplier_order` write different
    columns. Choosing between them on the side is the whole point; choosing
    wrong would put the supplier's rate in the client's field."""
    source = ast.unparse(_fn("send_order"))
    assert "create_client_order" in source
    assert "create_supplier_order" in source
    assert "FxSide.CLIENT" in source


def test_a_confirmation_from_the_wrong_group_is_ignored() -> None:
    """A callback carries whatever id it was built with. One confirmed from
    another room is not a confirmation, and this is money."""
    source = ast.unparse(_fn("counterparty_confirms"))
    assert "_group_for_side" in source
    assert "chat.id" in source


def test_the_deal_list_is_refused_outside_an_operations_group() -> None:
    """It carries both rates, which is the margin."""
    source = ast.unparse(_fn("list_deals"))
    assert "staff_context" in source
    assert "refusal_reason" in source


def test_the_commands_are_registered() -> None:
    assert cmd.ORDER_CLIENT in cmd.ALL
    assert cmd.ORDER_SUPPLIER in cmd.ALL
    assert cmd.FX_DEALS in cmd.ALL
    assert cmd.ORDER_CLIENT.startswith("np")
    assert cmd.ORDER_SUPPLIER.startswith("np")


def test_the_commands_are_claimed_by_the_fx_router() -> None:
    """Registration is checked here rather than through build_dispatcher().

    aiogram routers are singletons and refuse to attach to a second
    Dispatcher, so the dispatcher may only be built once in a process -
    test_wiring owns that one and checks the router order there. The router
    itself can be inspected directly as often as we like.
    """
    from aiogram.filters import Command

    registered = set()
    for handler in fx_handlers.router.message.handlers:
        for f in handler.filters or []:
            if isinstance(f.callback, Command):
                registered.update(str(c) for c in f.callback.commands)

    assert cmd.ORDER_CLIENT in registered
    assert cmd.ORDER_SUPPLIER in registered
    assert cmd.FX_DEALS in registered


async def test_a_deal_cannot_be_started_from_a_supplier_group(
    session, acme_support, support_ops, operator
):
    """A deal belongs to a client. Started from the supplier's side it would
    have the two halves the wrong way round, and the margin with them."""
    from app.bot.registry import register_client_chat
    from app.domain import work_items as wi
    from app.domain.enums import Department
    from app.domain.work_items import Actor

    pexi = await register_client_chat(
        session, telegram_chat_id=-1002000000077, client_name="Supplier Pexi",
        department=Department.SUPPORT, title="Pexi", is_supplier=True,
    )
    item = await wi.create_work_item(
        session, source_chat=pexi, subject="Rate",
        original_message="what can you do", raised_by_name="Gavin",
    )

    with pytest.raises(fx.FxError) as caught:
        await fx_handlers.start_deal(session, item, Actor.of(operator))

    assert "client's request" in str(caught.value)
