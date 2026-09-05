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

_DISPATCHER = None


def dispatcher():
    """The one dispatcher, built once and shared by every test here.

    aiogram routers are singletons and a Router refuses to attach to a second
    Dispatcher, so `build_dispatcher()` can only ever be called once in a
    process. Two tests both calling it is not a test problem - it is the same
    constraint that means exactly one bot instance may run, showing up early.
    """
    global _DISPATCHER
    if _DISPATCHER is None:
        from aiogram.fsm.storage.memory import MemoryStorage

        import app.bot.main as bot_main

        original, bot_main.build_storage = bot_main.build_storage, MemoryStorage
        try:
            _DISPATCHER = bot_main.build_dispatcher()
        finally:
            bot_main.build_storage = original
    return _DISPATCHER


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
    names = [r.name for r in dispatcher().sub_routers]
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


async def test_anonymous_admin_gets_a_useful_refusal():
    """A Telegram admin with 'Remain Anonymous' on posts as the group, so the
    bot cannot identify them. Saying 'not registered' sends them hunting for
    the wrong fix."""
    from app.bot import commands
    from app.bot.deps import ANONYMOUS_ADMIN_ID, is_anonymous_admin, refusal_reason

    assert is_anonymous_admin(ANONYMOUS_ADMIN_ID)
    assert not is_anonymous_admin(5001)

    anon = await refusal_reason(ANONYMOUS_ADMIN_ID)
    assert "anonymously" in anon.lower()
    assert "Remain Anonymous" in anon

    ordinary = await refusal_reason(5001)
    assert "not registered" in ordinary.lower()
    assert f"/{commands.ADDUSER}" in ordinary


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
    assert commands.RAISE == "npraise"


def test_no_command_contains_an_underscore() -> None:
    """NexterPay asked for them out on 3 September, having used np_raise for a week.

    Worth a test rather than a careful rename, because the next command added
    will be written by someone copying the shape of an existing one, and
    `_c("register_ops")` still reads perfectly naturally.
    """
    from app.bot import commands

    offenders = [n for n in commands.ALL if "_" in n]
    assert not offenders, f"these commands still carry an underscore: {offenders}"


def test_commands_are_answered_whatever_the_capitalisation() -> None:
    """People type /NPRAISE.

    Telegram does not normalise the case of a command and aiogram's filter is
    case-sensitive by default, so the wrong capitalisation is not a refusal -
    it is silence. Nothing reaches a handler, nothing is logged that anyone
    would think to look for, and the person concludes the bot is broken.
    """
    from app.bot import commands

    assert commands.any_case(commands.RAISE).ignore_case is True


async def test_a_staff_command_in_a_client_group_says_so(session, acme_support):
    """The refusal has to name the real problem.

    Reported in testing: someone sent a staff command in the client group and
    was told "you are not registered as active staff for this department".
    They then spent time hunting a permissions problem when they were simply
    in the wrong group.
    """
    from app.bot.deps import refusal_reason

    reason = await refusal_reason(1184638351, session, acme_support.telegram_chat_id)
    assert "client group" in reason.lower()
    assert "operations group" in reason.lower()
    assert "not registered as active staff" not in reason


async def test_an_unregistered_group_says_that_instead(session):
    from app.bot.deps import refusal_reason

    reason = await refusal_reason(1184638351, session, -1009999999999)
    assert "not registered" in reason.lower()
    assert "administrator needs to register it" in reason.lower()


async def test_the_wrong_department_names_both(session, support_ops, operator):
    """Staff belong to one department, which is not obvious when refused."""
    from app.bot.deps import refusal_reason
    from app.bot.registry import register_operations_chat
    from app.domain.enums import Department

    compliance_ops = await register_operations_chat(
        session, telegram_chat_id=-1001000000055,
        department=Department.COMPLIANCE, title="Compliance Operations",
    )
    reason = await refusal_reason(
        operator.telegram_user_id, session, compliance_ops.telegram_chat_id
    )
    assert "Support" in reason
    assert "Compliance and Risk" in reason


def test_no_message_shown_to_a_person_names_an_unprefixed_command() -> None:
    """The prefix has to reach the words people read, not just the handlers.

    The rename covered every Command filter but left "/adduser" sitting in the
    refusal message, so the bot spent a day telling people to run a command
    that no longer existed. The earlier guard could not catch it: it only
    inspected filters.

    Heuristic, and worth knowing its limit - it looks at lines carrying a
    quote character, so a command named only in a comment or docstring is not
    flagged. Those are for us to read, not for anyone using the bot.
    """
    import re

    from app.bot import commands

    # The bare form of each prefixed command, minus "start": npstart strips to
    # it, but /start is deliberately unprefixed and correct anywhere. The empty
    # string is dropped because FRONT_DOOR is the prefix itself.
    names = sorted(
        {n[len(commands.PREFIX):] for n in commands.ALL
         if n.startswith(commands.PREFIX)} - {commands.START, ""}
    )
    # scripts/ as well as app/. Restricting this to app/ is why preflight.py
    # was still printing "/adduser" a release after the rename - nothing a
    # person reads is safe just because it lives outside the bot package.
    sources = [
        *pathlib.Path("app").rglob("*.py"),
        *pathlib.Path("scripts").rglob("*.py"),
    ]
    offenders = []
    for path in sources:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or ('"' not in line and "'" not in line):
                continue
            for name in names:
                if re.search(rf"(?<!{commands.PREFIX})/{name}(?![\w_])", line):
                    offenders.append(f"{path}:{number}  {stripped[:80]}")

    assert not offenders, (
        "these messages name a command that no longer exists:\n  "
        + "\n  ".join(offenders)
    )


def test_no_prompt_forces_a_reply_from_nobody() -> None:
    """ForceReply(selective=True) without a real mention opens for nobody.

    This is the bug NexterPay reported as "broadcast did not work", and it had
    already been found and fixed once - in the client Raise Request flow, in
    August - and left in place in three other handlers. Telegram's `selective`
    means "force a reply from the users mentioned in this message". A name
    written as plain text is not a mention. So the prompt appears, no composer
    opens, the person waits, and the feature looks broken with nothing in the
    logs.

    Every prompt now goes through deps.prompt_for, which builds a tg://user
    link - a real text_mention entity - or falls back to selective=False. This
    test exists because the wrong version is the one that reads naturally.
    """
    import re

    offenders = []
    for path in pathlib.Path("app/bot").rglob("*.py"):
        if path.name == "deps.py":
            continue  # where prompt_for lives, and explains itself
        source = path.read_text()
        for number, line in enumerate(source.splitlines(), 1):
            if re.search(r"ForceReply\([^)]*selective\s*=\s*True", line):
                offenders.append(f"{path}:{number}  {line.strip()}")

    assert not offenders, (
        "these force a reply from nobody - use deps.prompt_for instead:\n  "
        + "\n  ".join(offenders)
    )


def test_every_command_the_bot_claims_has_a_handler() -> None:
    """`commands.ALL` is what the documents and /nphelp are generated from.

    A name in that list with no handler is a command the bot tells people to
    use and then ignores - and the failure is silence, which nobody reports as
    a bug because it looks like they typed it wrong.
    """
    from app.bot import commands

    dp = dispatcher()

    def walk(router):
        yield router
        for sub in router.sub_routers:
            yield from walk(sub)

    wired = set()
    for router in walk(dp):
        for handler in router.message.handlers:
            for f in handler.filters or []:
                values = getattr(getattr(f, "callback", None), "commands", None)
                for value in values or []:
                    wired.add(getattr(value, "pattern", value))

    missing = [c for c in commands.ALL if c not in wired]
    assert not missing, f"declared but nothing answers them: {missing}"

    undeclared = sorted(wired - set(commands.ALL))
    assert not undeclared, (
        f"handled but not in commands.ALL, so absent from the docs "
        f"and /nphelp: {undeclared}"
    )


def test_no_keyboard_builds_a_button_nobody_answers() -> None:
    """A dead button is the worst shape of bug this platform can produce.

    It looks live, it taps, and nothing happens - with nothing in the log,
    because no handler ran at all. This walks every keyboard in every shape
    and checks each payload against a handler prefix.
    """
    from app.bot import keyboards as keyboards
    from app.domain.enums import Department, StaffRole

    class _Lead:
        display_name, telegram_user_id = "Lead", 1

    class _Person:
        id, display_name, memberships = 3, "Sarah Hill", []

        def role_in(self, department):
            return StaffRole.OPERATOR

    class _Item:
        id, display_reference, subject = 7, "ACME-1000", "x"

    class _Counterparty:
        id, code, name = 1, "ACME", "Acme"

    every = [
        keyboards.work_item_actions(7, claimed=False),
        keyboards.work_item_actions(7, claimed=True),
        keyboards.work_item_actions(7, claimed=False, expanded=True),
        keyboards.confirm_reply(7),
        keyboards.confirm_reply(7, [_Lead()]),
        keyboards.closed_actions(7),
        keyboards.supplier_choices(7, [_Counterparty()]),
        keyboards.link_choices(7, [_Item()], [_Item()]),
        keyboards.department_choices(7, list(Department)),
        keyboards.confirm_internal(7, Department.FINANCE),
        keyboards.assignee_choices(7, [_Person()], Department.SUPPORT),
        keyboards.status_choices(7),
        keyboards.priority_choices(7),
        keyboards.acknowledgement_actions(),
        keyboards.raise_request_prompt("support"),
        keyboards.raise_request_prompt("business"),
        keyboards.setup_menu(in_operations=True, registered=True),
        keyboards.setup_menu(in_operations=False, registered=True),
        keyboards.setup_menu(in_operations=False, registered=False),
        keyboards.department_menu("regdept"),
        keyboards.role_menu(Department.SUPPORT),
    ]

    # Every prefix some callback_query handler claims.
    claimed = ("wi:", "ad:", "bc:", "ob:", "tk:", "raise:")
    orphans = [
        button.callback_data
        for markup in every
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and not button.callback_data.startswith(claimed)
    ]
    assert not orphans, f"these buttons reach no handler: {sorted(set(orphans))}"


def test_every_work_item_action_offered_is_one_that_is_handled() -> None:
    """The prefix being claimed is not enough - `wi:` reaches one handler that
    then branches on the action, and an unknown action falls through it
    silently returning "Done"."""
    import inspect
    import re

    from app.bot import keyboards as keyboards
    from app.bot.handlers import staff as staff_handlers
    from app.domain.enums import Department, StaffRole

    class _Person:
        id, display_name, memberships = 3, "Sarah Hill", []

        def role_in(self, department):
            return StaffRole.OPERATOR

    class _Item:
        id, display_reference, subject = 7, "ACME-1000", "x"

    class _Counterparty:
        id, code, name = 1, "ACME", "Acme"

    offered = set()
    for markup in [
        keyboards.work_item_actions(7, claimed=False),
        keyboards.work_item_actions(7, claimed=True),
        keyboards.work_item_actions(7, claimed=False, expanded=True),
        keyboards.confirm_reply(7),
        keyboards.closed_actions(7),
        keyboards.supplier_choices(7, [_Counterparty()]),
        keyboards.link_choices(7, [_Item()], [_Item()]),
        keyboards.department_choices(7, list(Department)),
        keyboards.confirm_internal(7, Department.FINANCE),
        keyboards.assignee_choices(7, [_Person()], Department.SUPPORT),
        keyboards.status_choices(7),
        keyboards.priority_choices(7),
    ]:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data.startswith("wi:"):
                    offered.add(button.callback_data.split(":")[1])

    source = inspect.getsource(staff_handlers._apply)
    handled = set(re.findall(r'action == "(\w+)"', source))
    for group in re.findall(r'action in \(([^)]*)\)', source):
        handled |= {v.strip().strip('"') for v in group.split(",") if v.strip()}

    assert not offered - handled, (
        f"offered on a keyboard but not handled: {sorted(offered - handled)}"
    )
