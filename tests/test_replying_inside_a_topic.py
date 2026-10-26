"""A command typed in a topic is not a reply to the topic.

Found live on 26 October. `/npremoveuser 1087968824`, sent in an Operations
topic, answered:

    Unknown department. One of: support|finance|development|business|
    compliance.

No department had been given. The id was read as one because something else
had already claimed the target slot: inside a forum topic the Bot API fills
`reply_to_message` on *every* message with the service message that opened
the topic, whether the person replied to anything or not. On this platform
the bot opens every topic, so every command typed in one looks like a reply
to the bot.

The consequences are not all as loud as that one:

  * `/npassign <telegram id>` needs a work item, so it is only ever typed in
    a topic, so the id is *always* ignored and it always tries to assign the
    bot. The documented form of the command could not work anywhere.
  * `/npadduser operator finance` in a topic tries to register the bot as
    staff rather than refusing.
  * `/npsetlead` in a topic names the bot as a group's lead.

The failure mode is the dangerous one. These commands do not break; they act
on the wrong person and then say something confident.

Messages are built with `model_construct` rather than by hand through the
constructor: what is being tested is how the handlers read fields Telegram
sets, and the point is to reproduce the exact shape Telegram sends.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram.types import Chat, ForumTopicCreated, Message, User

from app.bot.deps import replied_to_user

GROUP = Chat(id=-1001000000001, type="supergroup")
PETER = User(id=42, is_bot=False, first_name="Peter")
THE_BOT = User(id=777, is_bot=True, first_name="NexterPay Ops")
GAVS = User(id=8626894999, is_bot=False, first_name="Gavs")

WHEN = datetime.datetime(2026, 10, 26, 10, 6, tzinfo=datetime.UTC)


def _message(**fields) -> Message:
    return Message.model_construct(
        message_id=fields.pop("message_id", 500),
        date=WHEN,
        chat=GROUP,
        from_user=PETER,
        **fields,
    )


def test_the_topic_opening_is_not_somebody_you_replied_to() -> None:
    """The exact shape Telegram sends for a message typed in a topic."""
    topic_root = Message.model_construct(
        message_id=120,
        date=WHEN,
        chat=GROUP,
        from_user=THE_BOT,
        message_thread_id=120,
        forum_topic_created=ForumTopicCreated(name="SPEX-1125", icon_color=0x6FB9F0),
    )
    typed_in_the_topic = _message(
        text="/npremoveuser 1087968824",
        message_thread_id=120,
        is_topic_message=True,
        reply_to_message=topic_root,
    )

    assert replied_to_user(typed_in_the_topic) is None, (
        "a command typed in a topic was read as a reply to whoever opened "
        "it - which is always the bot, because the bot opens every topic"
    )


def test_a_topic_root_without_the_service_message_is_also_ignored() -> None:
    """The second check, and why there are two.

    `forum_topic_created` is exact and catches the normal case. A topic whose
    opening message is an ordinary message rather than a service one still
    has a root whose message id equals the thread id, and that is the case
    the first check cannot see.
    """
    root = Message.model_construct(
        message_id=120, date=WHEN, chat=GROUP, from_user=THE_BOT,
        message_thread_id=120, text="SPEX-1125 — opening",
    )
    typed = _message(
        text="/npassign 8626894999",
        message_thread_id=120,
        is_topic_message=True,
        reply_to_message=root,
    )

    assert replied_to_user(typed) is None


def test_a_real_reply_inside_a_topic_still_counts() -> None:
    """The fix must not remove the feature.

    Replying to a colleague's message *within* a topic is how `/npassign`
    is meant to be used, and that reply has a message id of its own that is
    not the thread id.
    """
    theirs = Message.model_construct(
        message_id=131, date=WHEN, chat=GROUP, from_user=GAVS,
        message_thread_id=120, text="I can take this one",
    )
    typed = _message(
        text="/npassign",
        message_thread_id=120,
        is_topic_message=True,
        reply_to_message=theirs,
    )

    assert replied_to_user(typed) is GAVS


def test_a_real_reply_outside_a_topic_still_counts() -> None:
    """`/npsetlead` is used in client groups, which have no topics at all."""
    theirs = Message.model_construct(
        message_id=88, date=WHEN, chat=GROUP, from_user=GAVS, text="morning",
    )
    typed = _message(text="/npsetlead", reply_to_message=theirs)

    assert replied_to_user(typed) is GAVS


def test_no_reply_at_all_is_nobody() -> None:
    assert replied_to_user(_message(text="/npremoveuser 1087968824")) is None


def test_a_reply_from_nobody_is_nobody() -> None:
    """Channel posts and anonymous admins arrive with no `from_user`."""
    anonymous = Message.model_construct(
        message_id=88, date=WHEN, chat=GROUP, from_user=None, text="posted",
    )
    assert replied_to_user(_message(text="/npassign", reply_to_message=anonymous)) is None


@pytest.mark.parametrize("module,name", [
    ("app/bot/handlers/admin.py", "cmd_adduser"),
    ("app/bot/handlers/admin.py", "cmd_removeuser"),
    ("app/bot/handlers/admin.py", "cmd_setlead"),
    ("app/bot/handlers/staff.py", "cmd_assign"),
])
def test_no_handler_reads_the_reply_itself(module, name) -> None:
    """The class, so the next one cannot reintroduce it.

    Four commands had their own copy of `message.reply_to_message.from_user`,
    and all four were wrong in the same way. Naming them here rather than
    scanning every handler is deliberate: a new command that means "the
    person you replied to" should fail this list by not being on it, which a
    person notices, rather than by passing a scan that never looked.
    """
    import ast
    import pathlib

    source = pathlib.Path(module).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            break
    else:
        raise AssertionError(f"{name} is gone from {module} - was it renamed?")

    body = ast.unparse(node)
    assert "reply_to_message" not in body, (
        f"{name} reads reply_to_message directly, so a command typed inside "
        f"a topic will act on whoever opened the topic"
    )
    assert "replied_to_user" in body, (
        f"{name} no longer resolves the person replied to at all"
    )
