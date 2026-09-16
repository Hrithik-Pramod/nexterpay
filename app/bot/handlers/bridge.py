"""Two-sided tickets: a request that runs between a client and a supplier.

Filing Structure and Connected Tickets, section 4, which NexterPay wrote on
30 August and which has been the thing everything else waited behind:

    A two-sided ticket has two: the client's group and the supplier's group,
    with your team in the middle and a single topic in the Operations Group
    where all of it is visible in one place. Your team therefore sees one
    conversation. The client sees their side of it. The supplier sees theirs.
    Neither sees the other.

**What this costs, said plainly.** Until now a request had exactly one outside
group, so sending to the wrong party was impossible by construction - there was
no code path that could do it. A second group deletes that guarantee. Section 4
sets out the four rules that replace it, and each one lives somewhere specific:

* *Nothing crosses automatically.* Nothing in this module forwards anything. A
  message arriving from one side lands in the Operations topic and stops there;
  somebody decides what to pass on, and types it.

* *Every outbound message names its destination before it is sent.* The reply
  flow asks which party first, and the confirmation carries their name rather
  than the word "send".

* *Nothing is sent without that confirmation.* Unchanged - it is the same
  preview every reply has always had.

* *The history records the direction of every message.* Inbound messages are
  recorded against the group they actually arrived in, and the topic says which
  side spoke.

The refusal that backs all of it is in `relay.send_client_reply`, not here: a
destination that is not a party to the request is rejected there, so a future
caller that forgets to ask cannot get it wrong either.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot import commands as cmd
from app.bot.deps import refusal_reason, staff_context, work_item_for_thread
from app.db.base import session_scope
from app.db.models import Chat, WorkItem
from app.domain.enums import ChatKind

logger = logging.getLogger(__name__)
router = Router(name="bridge")


async def bridgeable_chats(session, item: WorkItem, department) -> list[Chat]:
    """Groups this request could be opened to, on this desk.

    Its own group is excluded, which is not merely tidiness: bridging a request
    to the group it was raised in would give it two identical destinations and
    a side picker where both answers are the same.

    Scoped to the department for the same reason every other counterparty list
    is - a Support desk has no business bridging a request into a Finance
    supplier's group.
    """
    result = await session.execute(
        select(Chat).where(
            Chat.is_active.is_(True),
            Chat.kind == ChatKind.CLIENT,
            Chat.department == department,
            Chat.id != item.source_chat_id,
        ).order_by(Chat.is_supplier.desc(), Chat.title)
    )
    chats = list(result.scalars().all())
    for chat in chats:
        await session.refresh(chat, ["client"])
    return chats


def bridge_keyboard(item_id: int, chats: list[Chat]) -> InlineKeyboardMarkup:
    """Suppliers first, because that is what this is nearly always for.

    Clients are offered too - section 4 describes the case as a client and a
    supplier, but nothing in the design requires the second side to be either,
    and a request running between two clients is NexterPay's business rather
    than ours to forbid.
    """
    rows = [
        [InlineKeyboardButton(
            text=(
                f"{'supplier' if c.is_supplier else 'client'} · "
                f"{(c.client.code if c.client else '????')} · "
                f"{c.title or c.telegram_chat_id}"
            )[:60],
            callback_data=f"br:add:{item_id}:{c.telegram_chat_id}",
        )]
        for c in chats
    ]
    rows.append([InlineKeyboardButton(text="Cancel", callback_data="br:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def already_bridged(item: WorkItem, other_title: str | None) -> str:
    return (
        f"{item.display_reference} already runs with "
        f"{other_title or 'another group'}. A request has two sides at most — "
        f"the point of it is that each side sees only their own half, and a "
        f"third would have nowhere safe to sit."
    )


@router.message(cmd.any_case(cmd.BRIDGE))
async def cmd_bridge(message: Message) -> None:
    """`/npbridge` - open this request to a second group.

    Sent inside the request's own topic. It takes no reference deliberately: a
    reference typed from memory can be the wrong one, and on this command being
    wrong means pointing a client's conversation at a supplier.
    """
    user = message.from_user
    async with session_scope() as session:
        ctx = await staff_context(session, message.chat.id, user.id if user else None)
        if ctx is None:
            await message.reply(
                await refusal_reason(
                    user.id if user else None, session, message.chat.id
                )
            )
            return
        ops_chat, _ = ctx
        item = await work_item_for_thread(
            session, ops_chat, message.message_thread_id
        )
        if item is None:
            await message.reply(
                f"Send /{cmd.BRIDGE} inside the topic of the request you want "
                f"to open to a second group."
            )
            return

        if item.bridged_chat_id is not None:
            other = await session.get(Chat, item.bridged_chat_id)
            await message.reply(
                already_bridged(item, other.title if other else None)
            )
            return

        options = await bridgeable_chats(session, item, ops_chat.department)
        markup = bridge_keyboard(item.id, options) if options else None
        reference, department = item.display_reference, ops_chat.department.label

    if not options:
        await message.reply(
            f"No other groups are registered for {department}, so there is "
            f"nobody to open {reference} to."
        )
        return

    await message.reply(
        f"Which group should {reference} also run with?\n\n"
        f"They will see only what you send them, and nothing that has already "
        f"been said. Nothing crosses on its own.",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("br:add:"))
async def add_side(query: CallbackQuery) -> None:
    """Attach the second group.

    Sends nothing to anybody. Opening a request to a supplier is a decision
    about where it can go next, not a message - and a group that suddenly
    received "you have been added to ACME-1042" would be reading a reference
    that means nothing to them and a conversation they have not been part of.
    """
    _, _, item_id, chat_id = (query.data or "").split(":")

    async with session_scope() as session:
        ctx = await staff_context(
            session, query.message.chat.id,
            query.from_user.id if query.from_user else None,
        )
        if ctx is None:
            await query.answer("You are not registered as staff.", show_alert=True)
            return
        ops_chat, _ = ctx
        item = await session.get(WorkItem, int(item_id))
        if item is None:
            await query.answer("That request no longer exists.", show_alert=True)
            return
        if item.bridged_chat_id is not None:
            await query.answer("That one already has a second side.", show_alert=True)
            return

        # Re-resolved rather than trusted. A callback carries whatever id it was
        # built with, and this one decides which group a request can be sent to
        # from now on.
        options = await bridgeable_chats(session, item, ops_chat.department)
        chosen = next(
            (c for c in options if c.telegram_chat_id == int(chat_id)), None
        )
        if chosen is None:
            await query.answer(
                "That group is not one this desk can open a request to.",
                show_alert=True,
            )
            return

        item.bridged_chat_id = chosen.id
        await session.flush()
        logger.info(
            "Bridged %s to chat %s", item.display_reference, chosen.telegram_chat_id
        )
        reference, title = item.display_reference, chosen.title or str(chat_id)

    await query.answer("Opened.")
    await query.message.answer(
        f"{reference} now runs with {title} as well.\n\n"
        f"Reply to Client will ask which of them you mean, and the confirmation "
        f"names them before anything is sent. Nothing passes between the two "
        f"sides unless you send it."
    )


@router.callback_query(F.data == "br:cancel")
async def cancel(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.edit_text("Cancelled. Nothing was opened.")


__all__ = [
    "already_bridged",
    "bridge_keyboard",
    "bridgeable_chats",
    "router",
]
