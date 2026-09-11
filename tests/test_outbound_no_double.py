"""The outbound opening message must not say the same thing twice.

NexterPay's tester, 12 September, on the /npnewcl preview: "See the double
message". He was looking at the confirmation screen, where the text he had
typed appeared twice:

    — — —
    Issue with KYC reference 1231234

    Issue with KYC reference 1231234
    — — —

The preview was the visible half. The same duplication was in the message the
counterparty received, because both were built the same wrong way: the subject
is taken from the first line of the body, and then the subject line and the
whole body were printed one after the other. One typed line therefore went out
twice, and a multi-line message repeated its first line.

Only the preview was reported. The half that mattered was the one nobody saw,
which is the usual shape of these.

`outbound_body` is now the single answer to "what follows the subject line",
and both the preview and the real message ask it.
"""

from __future__ import annotations

import ast
import inspect

from app.services.relay import outbound_body

# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def test_a_single_line_is_not_repeated() -> None:
    """The reported case. The subject already carries these words."""
    typed = "Issue with KYC reference 1231234"
    assert outbound_body(typed, typed) == ""


def test_a_multi_line_message_keeps_everything_after_the_first_line() -> None:
    body = "Reconciliation\n\nChecking in on the March file."
    assert outbound_body("Reconciliation", body) == "Checking in on the March file."


def test_the_first_line_is_not_repeated_in_a_multi_line_message() -> None:
    body = "Reconciliation\nChecking in on the March file."
    rest = outbound_body("Reconciliation", body)
    assert rest.count("Reconciliation") == 0


def test_a_subject_that_did_not_come_from_the_body_keeps_the_body_whole() -> None:
    """`open_outbound` does not require the subject to come from the body.

    When it did not, dropping the first line would eat a line of what somebody
    typed - a far worse fault than repeating one.
    """
    body = "Please confirm the March settlement figures."
    assert outbound_body("Quarterly reconciliation", body) == body


def test_a_truncated_subject_keeps_the_body_whole() -> None:
    """A first line over 120 characters is truncated into the subject, so it
    no longer matches. The body must survive intact."""
    long_line = "x" * 200
    subject = long_line[:120]
    assert outbound_body(subject, long_line) == long_line


def test_an_empty_body_is_handled() -> None:
    assert outbound_body("New request", "") == ""


def test_whitespace_around_the_body_does_not_defeat_the_match() -> None:
    typed = "  Issue with KYC reference 1231234  "
    assert outbound_body("Issue with KYC reference 1231234", typed) == ""


# --------------------------------------------------------------------------
# The message the counterparty actually receives
# --------------------------------------------------------------------------

class _Item:
    """Enough of a work item to compose the opening message."""

    def __init__(self, subject: str) -> None:
        self.client_reference = "ACME-1051"
        self.subject = subject


def test_the_counterparty_sees_the_message_once() -> None:
    from app.services.relay import outbound_opening_text

    typed = "Issue with KYC reference 1231234"
    text = outbound_opening_text(_Item(typed), typed)

    assert text.count(typed) == 1, f"said twice:\n{text}"
    assert text.startswith("ACME-1051 · " + typed)
    assert text.endswith("Reply to this message to respond.")


def test_the_counterparty_still_gets_the_detail_of_a_longer_message() -> None:
    from app.services.relay import outbound_opening_text

    body = "Reconciliation\n\nChecking in on the March file."
    text = outbound_opening_text(_Item("Reconciliation"), body)

    assert text.count("Reconciliation") == 1
    assert "Checking in on the March file." in text


def test_there_is_no_empty_gap_when_there_is_nothing_after_the_subject() -> None:
    """A one-line request must not arrive with a hole punched in it."""
    from app.services.relay import outbound_opening_text

    typed = "Issue with KYC reference 1231234"
    text = outbound_opening_text(_Item(typed), typed)
    assert "\n\n\n" not in text


# --------------------------------------------------------------------------
# And that the preview cannot drift from it again
# --------------------------------------------------------------------------

def test_the_preview_composes_through_the_shared_function() -> None:
    """The preview exists to show what will be sent. Building it separately is
    what let it show something that was never going to be sent."""
    from app.bot.handlers import outbound as outbound_handlers

    tree = ast.parse(inspect.getsource(outbound_handlers))
    capture = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "capture"
    )
    source = ast.unparse(capture)
    assert "outbound_body" in source, (
        "the preview builds the message itself instead of asking the relay"
    )
