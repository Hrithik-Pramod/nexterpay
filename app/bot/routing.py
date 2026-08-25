"""Resolving an inbound client message to a work item.

NexterPay agreed the reply-to-acknowledgement mechanism: whenever the bot needs
something from a client it posts a message carrying the reference ("Request
#1042 - could you send the payment confirmation?"), and the client replies to
that message. Telegram gives us `reply_to_message`, so the work item is a
lookup rather than a guess.

The strategy is behind an interface for two reasons. The fallback behaviour for
a client who types a fresh message is still an open question with NexterPay,
and if they later decide the bot should be more forgiving we want to change one
binding rather than rework the relay.

`IncomingMessage` deliberately mirrors only the parts of an aiogram Message we
need, so this module - and its tests - stay free of Telegram objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, Message, WorkItem
from app.domain.work_items import open_items_for_chat


@dataclass(frozen=True)
class IncomingMessage:
    telegram_chat_id: int
    telegram_message_id: int
    sender_name: str
    sender_telegram_user_id: int | None = None
    text: str | None = None
    reply_to_message_id: int | None = None


class ReplyRoutingStrategy(ABC):
    """Given a message in a client group, which work item does it belong to?"""

    name: str

    @abstractmethod
    async def resolve(
        self, session: AsyncSession, chat: Chat, incoming: IncomingMessage
    ) -> WorkItem | None:
        """Return the work item, or None if it cannot be determined."""


class ReplyToAcknowledgementStrategy(ReplyRoutingStrategy):
    """The agreed mechanism.

    Resolves only when the client replied to a message the platform sent. If
    they typed something fresh, this returns None - which is correct, not a
    failure. The bot layer decides what to do with an unresolved message.
    """

    name = "reply_to_ack"

    async def resolve(
        self, session: AsyncSession, chat: Chat, incoming: IncomingMessage
    ) -> WorkItem | None:
        if incoming.reply_to_message_id is None:
            return None

        result = await session.execute(
            select(Message).where(
                Message.telegram_chat_id == incoming.telegram_chat_id,
                Message.telegram_message_id == incoming.reply_to_message_id,
            )
        )
        anchor = result.scalar_one_or_none()
        if anchor is None:
            return None
        return await session.get(WorkItem, anchor.work_item_id)


class MostRecentOpenItemStrategy(ReplyRoutingStrategy):
    """Fallback: attach to the most recently updated open item in this group.

    Not currently in use. Retained because it is the obvious alternative if
    NexterPay decides plain messages should be captured, and because having it
    written makes the trade-off concrete when that conversation happens: it is
    forgiving, and it is occasionally wrong in a way nobody notices.
    """

    name = "most_recent"

    async def resolve(
        self, session: AsyncSession, chat: Chat, incoming: IncomingMessage
    ) -> WorkItem | None:
        items = await open_items_for_chat(session, chat)
        return items[0] if items else None


class ChainedStrategy(ReplyRoutingStrategy):
    """Try each strategy in order, first non-None wins."""

    name = "chained"

    def __init__(self, *strategies: ReplyRoutingStrategy) -> None:
        self._strategies = strategies

    async def resolve(
        self, session: AsyncSession, chat: Chat, incoming: IncomingMessage
    ) -> WorkItem | None:
        for strategy in self._strategies:
            found = await strategy.resolve(session, chat, incoming)
            if found is not None:
                return found
        return None


_REGISTRY: dict[str, type[ReplyRoutingStrategy]] = {
    ReplyToAcknowledgementStrategy.name: ReplyToAcknowledgementStrategy,
    MostRecentOpenItemStrategy.name: MostRecentOpenItemStrategy,
}


def build_strategy(name: str) -> ReplyRoutingStrategy:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"Unknown routing strategy {name!r}; expected one of {sorted(_REGISTRY)}"
        ) from None
