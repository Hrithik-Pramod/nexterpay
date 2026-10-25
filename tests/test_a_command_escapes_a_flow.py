"""A slash command always escapes an open flow.

Found live on 25 October, driving the bot by hand in the TEST groups. Half way
through `/npsettle` - which waits for a pasted settlement block - typing
`/npbook` produced:

    No settlement lines found. Each line should read like "CI 50250000/583" -
    country, amount, and the rate it was converted at.

The command had been eaten as settlement data. `/npfx` typed in the very same
state worked, because its handler sits above the state handler in fx.py while
`/npbook`'s sits below it. Whether a command escaped a flow depended on where
somebody had happened to put a function in a file.

These tests drive a real Dispatcher rather than calling the middleware,
because the bug that nearly shipped with the fix is invisible to any test that
does not. `StateFilter` never reads the FSM storage - it reads the `raw_state`
value aiogram's own FSM middleware snapshotted into the handler data at update
level, before ours runs. Clear the context and leave the snapshot alone and
every assertion about `get_state()` passes while `/npbook` is still eaten.
Deleting the `data["raw_state"] = None` line from the middleware must turn
`test_a_command_escapes_the_flow_it_was_typed_into` red; if it does not, this
file has stopped testing the thing it exists for.

The handlers below are registered in the order that caused the live failure:
the greedy state handler first, the command second.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from app.bot import commands as cmd
from app.bot.main import KNOWN_COMMANDS, a_command_always_wins

CHAT_ID = -1002000009999
USER_ID = 777


class Flow(StatesGroup):
    """Stands in for FxSettle.awaiting_block and every flow like it."""

    awaiting_block = State()


class Harness:
    def __init__(self) -> None:
        self.storage = MemoryStorage()
        self.bot = Bot(token="42:AAHtesttesttesttesttesttesttesttest")
        self.dp = Dispatcher(storage=self.storage)
        self.dp.message.outer_middleware(a_command_always_wins)
        self.seen: list[tuple[str, str]] = []
        self._next_id = 0

        @self.dp.message(StateFilter(Flow.awaiting_block))
        async def swallows_anything(message: Message) -> None:
            self.seen.append(("flow", message.text or ""))

        @self.dp.message()
        async def the_command(message: Message) -> None:
            self.seen.append(("command", message.text or ""))

    @property
    def context(self) -> FSMContext:
        return FSMContext(
            storage=self.storage,
            key=StorageKey(bot_id=self.bot.id, chat_id=CHAT_ID, user_id=USER_ID),
        )

    async def send(self, text: str) -> None:
        self._next_id += 1
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self._next_id,
                message=Message(
                    message_id=self._next_id,
                    date=datetime.datetime.now(datetime.UTC),
                    chat=Chat(id=CHAT_ID, type="supergroup"),
                    from_user=User(id=USER_ID, is_bot=False, first_name="Peter"),
                    text=text,
                ),
            ),
        )

    async def close(self) -> None:
        await self.bot.session.close()


@pytest.fixture
async def desk():
    harness = Harness()
    try:
        yield harness
    finally:
        await harness.close()


async def test_a_command_escapes_the_flow_it_was_typed_into(desk: Harness) -> None:
    """The live failure of 25 October, as a test.

    This is the one that goes red if the `raw_state` half of the fix is lost.
    """
    await desk.context.set_state(Flow.awaiting_block)

    await desk.send(f"/{cmd.BOOK}")

    assert desk.seen == [("command", f"/{cmd.BOOK}")], (
        "the command was eaten by the open flow - which is what the desk saw "
        "live: /npbook answered with 'No settlement lines found'"
    )
    assert await desk.context.get_state() is None, (
        "the flow is still open, so the next thing typed is read as "
        "settlement data and the desk has no way out but a restart"
    )


async def test_the_flow_still_works_when_nothing_is_escaping_it(desk: Harness) -> None:
    """The guard must not break the flow it guards.

    A pasted settlement block is exactly what the state handler is for, and
    `/npsettle` is useless if this arrives anywhere else.
    """
    await desk.context.set_state(Flow.awaiting_block)

    await desk.send("CI 50250000/583\nSN 12000000/655")

    assert desk.seen == [("flow", "CI 50250000/583\nSN 12000000/655")]
    assert await desk.context.get_state() == Flow.awaiting_block.state


async def test_an_unknown_slash_word_does_not_abandon_the_flow(desk: Harness) -> None:
    """Only commands this bot answers to escape, not anything with a slash.

    Telegram groups carry other bots' commands and people mistype. A rule that
    fired on any leading slash would throw away a half-finished settlement
    because somebody typed `/stats` at a different bot in the same group - and
    the person losing the work would not be the person who typed.
    """
    await desk.context.set_state(Flow.awaiting_block)

    await desk.send("/notacommand")

    assert desk.seen == [("flow", "/notacommand")]
    assert await desk.context.get_state() == Flow.awaiting_block.state


async def test_the_forms_telegram_actually_delivers_all_escape(desk: Harness) -> None:
    """Shouted, addressed to the bot, and carrying arguments.

    Telegram appends `@thebot` to a command sent in a group with more than one
    bot in it, and `commands.any_case` exists because people type `/NPBOOK`.
    A guard that only recognised the bare lowercase form would be a guard that
    works in testing and fails in the group it was written for.
    """
    for text in (
        f"/{cmd.BOOK}".upper(),
        f"/{cmd.BOOK}@NexterPayOpsBot",
        f"/{cmd.HASH} FXACME-SPEX-1001 0xabc",
    ):
        desk.seen.clear()
        await desk.context.set_state(Flow.awaiting_block)

        await desk.send(text)

        assert desk.seen == [("command", text)], f"{text} was eaten by the flow"
        assert await desk.context.get_state() is None, f"{text} left the flow open"


async def test_a_command_outside_any_flow_is_left_alone(desk: Harness) -> None:
    """Nothing is cleared when there is nothing to clear."""
    await desk.send(f"/{cmd.BOOK}")

    assert desk.seen == [("command", f"/{cmd.BOOK}")]
    assert await desk.context.get_state() is None


def test_every_command_the_bot_answers_to_can_escape() -> None:
    """The escape list is derived, not maintained.

    A command added next year has to escape a flow without anybody remembering
    this file exists, so the list is built from `commands.ALL`. This asserts
    the derivation rather than restating the names - a list written out here
    would be one more place to forget.
    """
    assert KNOWN_COMMANDS == {name.lower() for name in cmd.ALL}
    assert cmd.BOOK in KNOWN_COMMANDS, "the command the live failure was found with"
    assert len(KNOWN_COMMANDS) == len(set(cmd.ALL)), "two commands differ only in case"
