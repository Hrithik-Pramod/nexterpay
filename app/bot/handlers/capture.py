"""Watching a counterparty group, and saying nothing in it.

The FX head's complaint, through Jason on 2 October, was that the platform
made him work differently: "too many stages... he wants to replicate what he
likes to do but automate the other". Asked on 4 October how much it should
interrupt him: "think working along side him", and that this should be the
primary way the desk works.

So this handler reads every message in a client or supplier group and
**produces nothing a counterparty can see**. What it understands appears in
the Operations Group, where the desk looks. A colleague keeping the ledger
does not interrupt the call you are on.

Three rules, and each of them is the kind of thing that is obvious until
somebody changes it.

**It never speaks in the counterparty's group.** Not a confirmation, not a
tick. The desk is mid-conversation with a client, and the platform joining in
is the thing being designed out.

**It never swallows the message.** Every path ends in `SkipHandler`, so the
existing routing - relaying what the client said, matching it to a request -
runs exactly as it did. This is an observer sitting in front of the real
handlers, not a replacement for them.

**It never records anything.** It offers a button. Primary is not the same as
automatic: Jason asked that the desk should not have to drive the platform,
not that the platform should commit NexterPay to figures nobody has read.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot import commands as cmd
from app.bot.deps import gateway
from app.bot.handlers.fx import FxSettle, settle_capture_block_from
from app.bot.registry import resolve_staff
from app.db.base import session_scope
from app.db.models import Chat
from app.domain.enums import ChatKind
from app.services import capture
from app.services.relay import _e

logger = logging.getLogger(__name__)
router = Router(name="capture")

# What the desk sees when the platform has understood something. Deliberately
# small and deliberately past-tense: it reports, it does not ask.
HEADING = "👀 <b>Seen in {where}</b>"


def observation_text(where: str, observations: list[capture.Observation]) -> str:
    """What the desk reads in their own group.

    Names the group it came from, because the desk runs several conversations
    at once and "a settlement arrived" is not useful without "from whom".
    """
    lines = [HEADING.format(where=_e(where)), ""]
    for seen in observations:
        lines.append(f"• {_e(seen.summary)}")
    lines += ["", "<i>Nothing has been recorded.</i>"]
    return "\n".join(lines)


def _action_for(observations: list[capture.Observation]) -> InlineKeyboardMarkup | None:
    """One button, for the one thing worth doing about it.

    Only offered for a settlement. A rate is worth knowing and not worth a
    button: it belongs to a deal that may not exist yet, and the desk quotes
    with `/npquote` where both sides of the price are entered together - which
    is the guard that stops anybody selling below cost.
    """
    if not any(seen.kind == "settlement" for seen in observations):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Record this settlement", callback_data="cap:settle"
        )],
        [InlineKeyboardButton(text="Ignore", callback_data="cap:ignore")],
    ])


async def _operations_for(session, chat: Chat) -> Chat | None:
    """The desk that owns this counterparty group.

    Matched on department, because that is what a counterparty group belongs
    to. None rather than a guess: posting a supplier's settlement into
    whichever Operations Group came first would put figures in front of a desk
    with no business seeing them.
    """
    result = await session.execute(
        select(Chat).where(
            Chat.kind == ChatKind.OPERATIONS,
            Chat.department == chat.department,
        )
    )
    return result.scalars().first()


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def watch(message: Message) -> None:
    """Read what was said, tell the desk, and get out of the way.

    Registered before the client router so it sees the message first, and
    ends in `SkipHandler` on every path so it changes nothing about what
    happens next.
    """
    from aiogram.dispatcher.event.bases import SkipHandler

    text = message.text or message.caption or ""
    observations = capture.observe(text)
    if not observations:
        raise SkipHandler

    async with session_scope() as session:
        result = await session.execute(
            select(Chat).where(Chat.telegram_chat_id == message.chat.id)
        )
        chat = result.scalars().first()
        if chat is None or chat.kind is not ChatKind.CLIENT:
            # Not a counterparty group. The desk's own messages in Operations
            # are not somebody else's conversation to observe.
            raise SkipHandler

        # Was this the desk talking? Then say nothing.
        #
        # Jason, 7 October: "when Slim sends a message normally, nothing
        # happens until they respond, for us, lots happens with the bot...
        # if he sends a message, the bot knows, but without an answer from
        # the client, its waiting. as he would when he is waiting for them."
        #
        # This handler read every message in a counterparty group without
        # looking at who sent it, so Slim posting a rate to a client had the
        # platform announce it straight back to him. He knows. He wrote it.
        #
        # Nothing is lost by the silence: the message is recorded either way,
        # and when the counterparty answers, that answer is observed and the
        # desk hears about it then - which is the moment something actually
        # changed.
        if message.from_user and await resolve_staff(session, message.from_user.id):
            logger.debug(
                "Desk message in %s - noted, not announced", message.chat.id
            )
            raise SkipHandler

        ops = await _operations_for(session, chat)
        if ops is None:
            logger.warning(
                "Saw something in %s with no Operations Group to tell",
                message.chat.id,
            )
            raise SkipHandler

        where = chat.title or ("the supplier" if chat.is_supplier else "the client")
        body = observation_text(where, observations)
        payload = next(
            (o.payload for o in observations if o.kind == "settlement"), None
        )
        ops_chat_id = ops.telegram_chat_id

    sent = await gateway().send_message(
        ops_chat_id, body,
        parse_mode="HTML",
        reply_markup=_action_for(observations),
    )

    if payload is not None:
        _PENDING[(ops_chat_id, sent.message_id)] = payload["text"]

    logger.info(
        "Observed %d thing(s) in chat %s", len(observations), message.chat.id
    )
    raise SkipHandler


# Blocks waiting for somebody to say yes, keyed by the message offering them.
#
# In memory on purpose. An observation is a prompt rather than a record - if
# the bot restarts before anybody taps, the block is still sitting in the
# supplier's group and can be pasted into `/npsettle` exactly as it always
# could. Persisting it would mean a table of things nobody acted on.
_PENDING: dict[tuple[int, int], str] = {}


@router.callback_query(F.data == "cap:ignore")
async def ignore(query: CallbackQuery) -> None:
    """The desk has seen it and does not want it recorded.

    Worth having. A platform that only offers yes teaches people to leave
    prompts sitting there, and a column of unanswered prompts is how somebody
    stops reading them.
    """
    await query.answer()
    _PENDING.pop((query.message.chat.id, query.message.message_id), None)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear the observation buttons", exc_info=True)


@router.callback_query(F.data == "cap:settle")
async def record(query: CallbackQuery, state: FSMContext) -> None:
    """Hand the block to the settlement flow the desk already uses.

    Deliberately the same flow as `/npsettle` rather than a shortcut of its
    own. The preview, the matching, the discrepancy check and the refusal to
    guess between two deals are the whole value of that flow, and a capture
    path that skipped them would be a second, quieter way to record a payment.
    """
    key = (query.message.chat.id, query.message.message_id)
    block = _PENDING.pop(key, None)
    await query.answer()

    # Claimed before the work, like every other button on this platform.
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not clear the observation buttons", exc_info=True)

    if block is None:
        await query.message.answer(
            "That one has already been dealt with, or the bot has restarted "
            "since. The block is still in their group — paste it into "
            f"/{cmd.SETTLE}."
        )
        return

    await state.clear()
    await state.set_state(FxSettle.awaiting_block)
    await query.message.answer(
        "Reading it as if you had pasted it:"
    )
    # `query.message` is the bot's own observation post, so it says where to
    # reply but not who is asking. `query.from_user` is the person who tapped,
    # and the settlement flow needs them to check they are staff.
    await settle_capture_block_from(
        query.message, state, block, actor=query.from_user
    )


__all__ = ["HEADING", "observation_text", "record", "router", "watch"]
