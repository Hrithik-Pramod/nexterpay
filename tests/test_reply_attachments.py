"""Sending a file to a client from the Reply to Client button.

NexterPay, Report 4: "Cannot send from Company to client a screenshot. For
attaching anything, you must type first then attach attachment."

Accurate, and the shape of it mattered more than the report suggested. There
were two routes outward and only one of them carried files:

* `/npreply` as a caption on the file — worked, and had a test since PRD 7.5.
* The Reply to Client button — kept the caption and dropped the file, silently.

So a member of staff attached a screenshot, typed a caption, saw "Sent to the
client", and the client received the words alone. The service layer was never
at fault: `send_client_reply` has taken an attachment all along, and
`test_staff_attachment_reaches_the_client` proves it. The handler simply never
passed one.

That is the same gap that hid the broadcast bug in August and the raising bug
in September: 433 tests at the service layer, and nothing where a handler meets
Telegram. Hence `reply_body`, which is the decision that was wrong, pulled out
where it can be called.
"""

from __future__ import annotations

import ast
import inspect

from app.bot.handlers.staff import reply_body

# --------------------------------------------------------------------------
# The decision itself
# --------------------------------------------------------------------------

def test_words_alone_are_sent_as_typed() -> None:
    assert reply_body("the payment cleared at 14:02", has_file=False) == (
        "the payment cleared at 14:02"
    )


def test_a_file_with_a_caption_keeps_the_caption() -> None:
    """The caption is what the person wrote. It is not replaced."""
    assert reply_body("proof of settlement", has_file=True) == "proof of settlement"


def test_a_file_with_no_caption_still_goes_out() -> None:
    """This is the case that was refused outright.

    A screenshot with nothing typed is a perfectly ordinary thing to send, and
    "type the message the client should see" is a strange answer to it.
    """
    body = reply_body("", has_file=True)
    assert body is not None
    assert body.strip() != ""


def test_nothing_at_all_is_still_refused() -> None:
    """The guard that was right: an empty reply is not a reply."""
    assert reply_body("", has_file=False) is None
    assert reply_body(None, has_file=False) is None
    assert reply_body("   ", has_file=False) is None


def test_whitespace_with_a_file_does_not_send_a_blank_line() -> None:
    """A space bar pressed by accident must not become the client's message."""
    assert reply_body("   ", has_file=True) == "please see the attached."


# --------------------------------------------------------------------------
# And that the handler actually looks for a file
#
# `reply_body` being right is worth nothing if the handler never asks whether
# there is an attachment, which is precisely the state the code was in.
# --------------------------------------------------------------------------

def _fn(name: str) -> ast.FunctionDef:
    from app.bot.handlers import staff

    tree = ast.parse(inspect.getsource(staff))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node) -> set[str]:
    return {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_the_draft_handler_looks_for_an_attachment() -> None:
    assert "extract_attachments" in _calls(_fn("capture_reply_draft")), (
        "capture_reply_draft does not look for a file, so it cannot carry one"
    )


def test_the_draft_handler_uses_the_shared_decision() -> None:
    """Rather than re-deriving "is this sendable" and drifting from it."""
    assert "reply_body" in _calls(_fn("capture_reply_draft"))


def test_the_draft_is_stored_with_its_attachment() -> None:
    """The preview and the send are separate messages, so the file has to
    survive in state between them. It is the step that was missing."""
    source = ast.unparse(_fn("capture_reply_draft"))
    assert "draft_attachment" in source, "the attachment is not kept for the send"


def test_sending_passes_the_attachment_on() -> None:
    """The last link: the confirm branch must hand it to the relay.

    Checked on the whole action handler because the send lives in a branch of
    `_apply`, and naming the branch would make this test a description of the
    current structure rather than of the behaviour.
    """
    source = ast.unparse(_fn("_apply"))
    assert "draft_attachment" in source, "the send never reads the stored attachment"
    assert "attachment=attachment" in source.replace(" ", ""), (
        "send_client_reply is called without the attachment"
    )
