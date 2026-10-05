"""Handlers that fire on a plain group message, enumerated.

On 5 October the bot was promoted to administrator in the FX groups, because
the capture layer cannot read a conversation it is not shown. Promotion does
not change a line of this codebase. It changes *what arrives*, and two
handlers written under the old assumption were wrong within minutes of it:

  * `client_reply` answered a reply aimed at somebody else, telling two
    colleagues the platform could not match their conversation to a request;
  * `offer_lookup_format` corrected the FX desk on its own figures, because
    a ten-digit amount looks exactly like a reference.

Neither failed a test. Both were about messages that had simply never reached
the bot before, and 1000-odd tests say nothing about that - they all start
from the message already being in the handler.

So this file enumerates the handlers that fire on an unfiltered group
message. A new one is a new decision, and the decision is always the same
question: what does this do now the bot sees everything? Adding one without
answering that is how the next pair of these gets shipped.

Command handlers are excluded because a command always reached the bot.
Handlers filtered on FSM state are excluded because state is keyed on chat
and user together, so one only fires for somebody already mid-flow.
"""

from __future__ import annotations

import ast
import pathlib

HANDLERS = pathlib.Path("app/bot/handlers")

# Every handler that fires on a group message without a command or a state to
# narrow it, and what makes each one safe now the bot is an administrator.
KNOWN_OPEN_HANDLERS = {
    # Nudges somebody who pasted a reference with no command. Now also sees
    # the desk's own figures, so `looks_like_desk_traffic` excludes settlement
    # lines, amounts beside a currency, and rates written as a fraction.
    "offer_lookup_format",
    # Relays what a counterparty says. Now also sees two of them replying to
    # each other, so the unrouted notice is only sent when the reply was aimed
    # at a message we actually sent - `our_message_behind`.
    "client_reply",
    # Guarded on the chat being an Operations Group, and raises SkipHandler
    # otherwise. Telegram sets message_thread_id on any supergroup reply, not
    # only in forums, so this guard predates the promotion and still holds.
    "topic_message",
    # Edits, guarded the same way: not an Operations Group, SkipHandler.
    "staff_edited_a_message",
}


def _open_message_handlers() -> set[str]:
    found = set()
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                source = ast.unparse(decorator)
                if ".message(" not in source and ".edited_message(" not in source:
                    continue
                # A command always reached the bot, promoted or not.
                if "cmd." in source or "any_case" in source:
                    continue
                # FSM state is keyed on chat and user, so it only fires for
                # somebody already part-way through a flow they started.
                if "awaiting" in source or "States" in source:
                    continue
                found.add(node.name)
    return found


def test_the_open_handlers_are_the_ones_we_have_thought_about() -> None:
    """A new one is a new decision, not a new line.

    If this fails, the question to answer is not "how do I make the test
    pass" but "what does my handler do now that it receives every message in
    a client's group, including ones nobody meant for us". Then add it to the
    set above with the answer written beside it.
    """
    found = _open_message_handlers()

    unexpected = sorted(found - KNOWN_OPEN_HANDLERS)
    assert not unexpected, (
        f"these handlers fire on any group message and have not been "
        f"considered against an administrator bot: {unexpected}. Promotion "
        f"does not change the code - it changes what arrives, which is "
        f"exactly what the rest of the suite cannot see."
    )

    gone = sorted(KNOWN_OPEN_HANDLERS - found)
    assert not gone, (
        f"these are listed as open handlers but no longer exist: {gone}. An "
        f"entry for something that has been renamed stops guarding anything "
        f"and starts hiding whatever replaced it."
    )


def test_the_two_that_were_wrong_still_carry_their_guards() -> None:
    """Named outright, because these are the two that actually broke.

    Checked on the source rather than by driving aiogram, which is how the
    rest of this project's handler guards are written - and the reason both
    faults were invisible is that no test could reach the handler at all.
    """
    client = (HANDLERS / "client.py").read_text(encoding="utf-8")

    nudge = client[client.index("def should_nudge"):]
    nudge = nudge[: nudge.index("\n@router")] if "\n@router" in nudge else nudge
    assert "looks_like_desk_traffic(text)" in nudge

    reply = client[client.index("async def client_reply"):]
    assert reply.index("our_message_behind(") < reply.index("unrouted_notice(")


def test_the_operations_only_guards_skip_rather_than_return() -> None:
    """SkipHandler hands the message on; return swallows it.

    `topic_message` is filtered on message_thread_id, which Telegram sets on
    any supergroup reply and not only in forums - so a counterparty replying
    in their own group reaches it. Returning there would consume the update
    before the client router ever saw it, which is the silent-loss failure
    this project has already paid for once.
    """
    staff = (HANDLERS / "staff.py").read_text(encoding="utf-8")

    topic = staff[staff.index("async def topic_message"):]
    topic = topic[: topic.index("@router")]
    assert "if not is_operations:" in topic
    assert "raise SkipHandler" in topic

    edited = staff[staff.index("async def staff_edited_a_message"):]
    edited = edited[: edited.index("@router")] if "@router" in edited else edited
    assert "raise SkipHandler" in edited
