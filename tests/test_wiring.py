"""Wiring checks.

These are cheap and they catch the class of mistake that only shows up when the
bot is live: a router registered in the wrong order, a callback that no handler
claims, a keyboard whose payload nobody can parse.
"""

from __future__ import annotations

import pathlib

import pytest

from app.bot import keyboards as kb
from app.bot.deps import work_item_for_thread
from app.domain.enums import Priority, WorkItemStatus


def test_callback_payloads_round_trip():
    for action in ["claim", "status", "priority", "close", "history", "back"]:
        assert kb.parse_cb(kb.cb(action, 42)) == (action, 42, None)

    for status in WorkItemStatus:
        data = kb.cb("setstatus", 1042, status.value)
        assert len(data.encode()) <= 64, "Telegram caps callback data at 64 bytes"
        assert kb.parse_cb(data) == ("setstatus", 1042, status.value)

    for priority in Priority:
        assert kb.parse_cb(kb.cb("setpriority", 7, priority.value))[2] == priority.value


def test_malformed_callback_is_rejected():
    with pytest.raises(ValueError):
        kb.parse_cb("garbage")
    with pytest.raises(ValueError):
        kb.parse_cb("other:claim:1")


def test_business_groups_get_the_commercial_wording():
    """PRD 15.4 - Business Operations is a commercial workflow, not a queue."""
    business = kb.raise_request_prompt("business")
    support = kb.raise_request_prompt("support")

    assert business.inline_keyboard[0][0].text == "Commercial Enquiry"
    assert support.inline_keyboard[0][0].text == "Raise Request"


def test_every_action_button_has_a_parseable_payload():
    markup = kb.work_item_actions(1042, claimed=False)
    for row in markup.inline_keyboard:
        for button in row:
            action, work_item_id, _ = kb.parse_cb(button.callback_data)
            assert work_item_id == 1042
            assert action


def test_claim_becomes_reassign_once_owned():
    unclaimed = kb.work_item_actions(1, claimed=False).inline_keyboard[0][0]
    claimed = kb.work_item_actions(1, claimed=True).inline_keyboard[0][0]

    assert unclaimed.text == "Claim"
    assert claimed.text == "Reassign"


def test_routers_are_ordered_so_the_catch_all_is_last():
    """The client router ends in a catch-all for group messages. If it were
    registered before the staff router, staff commands would fall into it."""
    from app.bot.main import build_dispatcher

    names = [r.name for r in build_dispatcher().sub_routers]
    assert names == ["admin", "broadcast", "outbound", "staff", "client", "trace"]
    # "trace" logs anything no one else wanted, so it must stay behind the
    # client catch-all or it would log every ordinary message as unhandled.
    assert names[-1] == "trace"
    assert names.index("client") > names.index("staff")
    # Composing a broadcast is answered as a reply; in a forum that carries a
    # thread id, so it must be offered the message before topic_message is.
    assert names.index("broadcast") < names.index("staff")
    assert names.index("outbound") < names.index("staff")


async def test_topic_maps_back_to_its_work_item(session, acme_support, support_ops):
    from app.services.gateway import FakeGateway
    from app.services.relay import open_request

    gw = FakeGateway()
    item = await open_request(
        session, gw,
        source_chat=acme_support,
        subject="Settlement",
        body="body",
        raised_by_name="Tom Baker",
    )

    found = await work_item_for_thread(session, support_ops, item.topic_id)
    assert found is not None and found.id == item.id

    assert await work_item_for_thread(session, support_ops, 999999) is None
    assert await work_item_for_thread(session, support_ops, None) is None


def test_handler_modules_import_cleanly():
    """Catches import-time errors in handlers, which polling would only reveal
    on the first matching update."""
    from app.bot.handlers import admin, client, staff

    assert admin.router.name == "admin"
    assert client.router.name == "client"
    assert staff.router.name == "staff"


def test_anonymous_admin_gets_a_useful_refusal():
    """A Telegram admin with 'Remain Anonymous' on posts as the group, so the
    bot cannot identify them. Saying 'not registered' sends them hunting for
    the wrong fix."""
    from app.bot.deps import ANONYMOUS_ADMIN_ID, is_anonymous_admin, refusal_reason

    assert is_anonymous_admin(ANONYMOUS_ADMIN_ID)
    assert not is_anonymous_admin(5001)

    anon = refusal_reason(ANONYMOUS_ADMIN_ID)
    assert "anonymously" in anon.lower()
    assert "Remain Anonymous" in anon

    ordinary = refusal_reason(5001)
    assert "not registered" in ordinary.lower()
    assert "/adduser" in ordinary


async def test_anonymous_admin_is_never_treated_as_staff(session, support_ops, senior):
    """Even if the group itself were somehow registered, an anonymous poster
    must not inherit anyone's permissions."""
    from app.bot.deps import ANONYMOUS_ADMIN_ID, staff_context

    ctx = await staff_context(
        session, support_ops.telegram_chat_id, senior.telegram_user_id
    )
    assert ctx is not None, "a real staff member should resolve"

    anon = await staff_context(
        session, support_ops.telegram_chat_id, ANONYMOUS_ADMIN_ID
    )
    assert anon is None


def test_raise_prompt_forces_a_reply() -> None:
    """The Raise Request prompt must use ForceReply.

    This is not a style preference. The bot runs with privacy mode ON and is
    deliberately not an administrator in client groups, so Telegram delivers
    only commands and replies to the bot's own messages. The description step
    is neither - unless the client replies. ForceReply is what makes it a
    reply.

    Regression: this was originally a plain send. It appeared to work only
    because the bot was temporarily an admin in the test client group, which
    bypasses privacy mode. The moment the bot was correctly demoted to an
    ordinary member, raising a request stopped producing a work item and
    failed completely silently - the update never reached the bot at all, so
    there was nothing in the logs either.
    """
    import inspect

    from app.bot.handlers import client as client_handlers

    source = inspect.getsource(client_handlers.start_request)
    assert "ForceReply" in source, (
        "start_request no longer forces a reply. Under privacy mode the "
        "client's description will never reach the bot and Raise Request "
        "will fail silently."
    )


def test_unreachable_redis_falls_back_to_memory(monkeypatch) -> None:
    """An unreachable Redis must degrade the bot, not stop it.

    Regression: FSM state was moved to Redis behind a try/except around
    RedisStorage.from_url(). That call only builds a client and never
    connects, so the except branch was unreachable. The real connection
    happened inside aiogram's FSM middleware, which runs before every
    handler on every update - so with Redis down the bot polled happily and
    answered nothing at all, in any group, for any user.
    """
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import main as bot_main

    monkeypatch.setattr(bot_main, "_reachable", lambda *_args, **_kw: False)
    assert isinstance(bot_main.build_storage(), MemoryStorage)


def test_reachable_redis_is_used(monkeypatch) -> None:
    import pytest

    pytest.importorskip("redis", reason="runtime dependency, not installed for tests")
    from app.bot import main as bot_main

    monkeypatch.setattr(bot_main, "_reachable", lambda *_args, **_kw: True)
    storage = bot_main.build_storage()
    assert type(storage).__name__ == "RedisStorage"


def test_the_trace_router_never_logs_message_content() -> None:
    """Diagnostics must not put clients' words in the server logs.

    NexterPay agreed to make the bot an administrator in client groups, which
    means it now receives every message there rather than only replies to
    itself. The unhandled-message diagnostic was written before that and
    logged a snippet of each message. Under admin rights that would quietly
    record fragments of clients' private conversation.

    A leading command is still logged - commands are not private and are the
    thing worth diagnosing.
    """
    import inspect

    from app.bot import main as bot_main

    source = inspect.getsource(bot_main.build_dispatcher)
    trace = source[source.index("trace = Router"):]

    assert "message.text or message.caption" in trace, "expected the body to be read"
    assert "chars=%d" in trace, "length should be logged instead of content"
    assert "text=%r" not in trace, (
        "the trace router is logging message content again; with the bot as an "
        "administrator that writes clients' conversation into the server logs"
    )


def test_every_command_carries_the_np_prefix() -> None:
    """No command may be added without the prefix.

    NexterPay run a second bot in the same client groups. Telegram delivers a
    bare command to whichever bot posted last, and if both bots implement the
    same name both of them answer - admin rights fix the first problem but not
    the second. The prefix removes both, and only holds if it is universal.

    `/start` is the single deliberate exception: Telegram's own interface
    sends it when someone taps Start, so it has to keep working under that
    name. `/np_start` is registered alongside it.
    """
    from aiogram.filters import Command

    from app.bot import commands
    from app.bot.handlers import admin, client, staff

    # The routers are module-level singletons and cannot be attached to a
    # second dispatcher, so they are inspected directly rather than through
    # build_dispatcher().
    registered = set()
    for router in (admin.router, staff.router, client.router):
        for handler in router.message.handlers:
            for f in handler.filters or []:
                if isinstance(f.callback, Command):
                    registered.update(str(c) for c in f.callback.commands)

    assert registered, "no commands registered at all"
    unprefixed = {n for n in registered if not n.startswith(commands.PREFIX)}
    assert not unprefixed, (
        f"these commands are missing the {commands.PREFIX} prefix: "
        f"{sorted(unprefixed)}. A bare command collides with NexterPay's other bot."
    )
    assert commands.FRONT_DOOR in registered


def test_no_command_name_is_hard_coded() -> None:
    """Names must come from app/bot/commands.py, not string literals.

    A literal is how the prefix gets lost: someone adds Command("export") in a
    hurry, it works in testing because our bot is the only one in the room,
    and it fails intermittently in a client group months later.
    """
    import re

    from app.bot import commands

    offenders = []
    for path in pathlib.Path("app/bot").rglob("*.py"):
        for line, text in enumerate(path.read_text().splitlines(), 1):
            for literal in re.findall(r'Command\(\s*"([^"]+)"', text):
                if literal != commands.START:
                    offenders.append(f"{path}:{line} Command(\"{literal}\")")
    assert not offenders, (
        "command names must be referenced from app/bot/commands.py:\n  "
        + "\n  ".join(offenders)
    )


def test_the_front_door_is_just_np() -> None:
    from app.bot import commands

    assert commands.FRONT_DOOR == "np"
    assert commands.RAISE == "np_raise", "underscore form was agreed with the client"
