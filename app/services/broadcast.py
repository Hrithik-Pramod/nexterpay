"""Broadcasting: one message to many counterparty groups.

Kept in its own module rather than added to the relay, because it breaks the
relay's central rule. Everything there writes to one client chat, the one a
request came from, which is what makes sending to the wrong party impossible
by construction. A broadcast writes to every counterparty at once, so it needs
its own guarantees rather than borrowing ones that no longer apply.

Those guarantees are:

* Manager and above. Reassigning a single ticket needs Senior Operator, so
  messaging every client at once should not be available more widely.
* Nothing sends without someone seeing the exact text, the audience, and the
  number of groups, and confirming.
* Every recipient is recorded, including the ones that failed. A bot removed
  from a group fails quietly, and "it went to everyone" would be a lie.
* It can be taken back for 48 hours, which is how long Telegram will let a bot
  delete its own messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Broadcast, BroadcastDelivery, Chat
from app.domain.enums import ChatKind, StaffRole
from app.domain.errors import DomainError
from app.domain.work_items import Actor, utcnow
from app.services.gateway import TelegramGateway

logger = logging.getLogger(__name__)

ROLE_REQUIRED_TO_BROADCAST = StaffRole.MANAGER

# Telegram will only delete a bot's own message within 48 hours.
RECALL_WINDOW = timedelta(hours=48)

EVERYONE = "everyone"
CLIENTS = "clients"
SUPPLIERS = "suppliers"
SELECTED = "selected"

AUDIENCE_LABELS = {
    EVERYONE: "all clients and suppliers",
    CLIENTS: "all clients",
    SUPPLIERS: "all suppliers",
    SELECTED: "selected groups",
}


@dataclass(frozen=True)
class Recipient:
    telegram_chat_id: int
    title: str


async def audience_for(
    session: AsyncSession, audience: str, chat_ids: list[int] | None = None
) -> list[Recipient]:
    """The groups a broadcast would reach, resolved before anything is sent.

    Operations Groups are never included. A broadcast is for counterparties;
    staff already read the group it is composed in.
    """
    query = select(Chat).where(
        Chat.is_active.is_(True), Chat.kind == ChatKind.CLIENT
    )
    if audience == CLIENTS:
        query = query.where(Chat.is_supplier.is_(False))
    elif audience == SUPPLIERS:
        query = query.where(Chat.is_supplier.is_(True))
    elif audience == SELECTED:
        query = query.where(Chat.telegram_chat_id.in_(chat_ids or []))
    elif audience != EVERYONE:
        raise DomainError(f"Unknown audience {audience!r}")

    result = await session.execute(query.order_by(Chat.title))
    return [
        Recipient(
            telegram_chat_id=c.telegram_chat_id,
            title=c.title or str(c.telegram_chat_id),
        )
        for c in result.scalars().all()
    ]


async def broadcast_behind(
    session: AsyncSession, telegram_chat_id: int, telegram_message_id: int
) -> Broadcast | None:
    """The broadcast a client is replying to, if that is what they replied to.

    Returns the record rather than a yes/no, because whoever picks the
    resulting request up needs to see what it was a reply to. A ticket that
    says only "why so?" is unworkable.
    """
    result = await session.execute(
        select(Broadcast)
        .join(BroadcastDelivery, BroadcastDelivery.broadcast_id == Broadcast.id)
        .where(
            BroadcastDelivery.telegram_chat_id == telegram_chat_id,
            BroadcastDelivery.telegram_message_id == telegram_message_id,
        )
    )
    return result.scalar_one_or_none()


async def was_broadcast(
    session: AsyncSession, telegram_chat_id: int, telegram_message_id: int
) -> bool:
    """Is this message one we broadcast into that group?

    A broadcast is not a ticket, so a reply to one resolves to nothing under
    the normal routing. NexterPay chose that such a reply should open a fresh
    request rather than be lost - "best be safe" - which needs a way to tell a
    broadcast apart from any other message the client happens to reply to.
    """
    result = await session.execute(
        select(BroadcastDelivery.id).where(
            BroadcastDelivery.telegram_chat_id == telegram_chat_id,
            BroadcastDelivery.telegram_message_id == telegram_message_id,
        )
    )
    return result.scalar_one_or_none() is not None


def preview(body: str, audience: str, recipients: list[Recipient]) -> str:
    """What the sender sees before deciding. Deliberately blunt."""
    names = ", ".join(r.title for r in recipients[:12])
    if len(recipients) > 12:
        names += f", and {len(recipients) - 12} more"
    return (
        f"This will be sent to {len(recipients)} group"
        f"{'' if len(recipients) == 1 else 's'} "
        f"({AUDIENCE_LABELS.get(audience, audience)}):\n"
        f"{names}\n\n"
        f"— — —\n{body}\n— — —\n\n"
        f"Nothing has been sent yet."
    )


async def send(
    session: AsyncSession,
    gateway: TelegramGateway,
    *,
    body: str,
    audience: str,
    recipients: list[Recipient],
    actor: Actor,
) -> Broadcast:
    """Send it, and record what actually happened to each group."""
    actor.require(ROLE_REQUIRED_TO_BROADCAST)
    if not body.strip():
        raise DomainError("A broadcast needs a message.")
    if not recipients:
        raise DomainError("That audience has no groups in it.")

    record = Broadcast(
        sent_by_staff_id=actor.staff.id if actor.staff else None,
        sent_by_name=actor.name,
        audience=audience,
        body=body,
    )
    session.add(record)
    await session.flush()

    for recipient in recipients:
        delivery = BroadcastDelivery(
            broadcast_id=record.id,
            telegram_chat_id=recipient.telegram_chat_id,
            chat_title=recipient.title,
        )
        try:
            sent = await gateway.send_message(recipient.telegram_chat_id, body)
            delivery.telegram_message_id = sent.message_id
        except Exception as exc:
            # One group failing must not stop the rest, and must not be
            # silent. A bot removed from a group is the common case.
            logger.warning(
                "Broadcast %s failed for %s: %s", record.id, recipient.title, exc
            )
            delivery.error = str(exc)[:300]
        session.add(delivery)

    await session.flush()
    return record


def outcome(record: Broadcast, deliveries: list[BroadcastDelivery]) -> str:
    delivered = [d for d in deliveries if d.error is None]
    failed = [d for d in deliveries if d.error is not None]
    lines = [f"Broadcast sent to {len(delivered)} of {len(deliveries)} groups."]
    if failed:
        lines.append("")
        lines.append("Did not arrive:")
        lines += [f"  {d.chat_title} — {d.error}" for d in failed]
    return "\n".join(lines)


async def deliveries_for(
    session: AsyncSession, record: Broadcast
) -> list[BroadcastDelivery]:
    result = await session.execute(
        select(BroadcastDelivery)
        .where(BroadcastDelivery.broadcast_id == record.id)
        .order_by(BroadcastDelivery.id)
    )
    return list(result.scalars().all())


async def recall(
    session: AsyncSession, gateway: TelegramGateway, record: Broadcast, actor: Actor
) -> str:
    """Delete a broadcast from the groups it reached, where Telegram allows.

    Beyond 48 hours Telegram refuses, and there is nothing to be done about
    that - which is worth saying plainly rather than reporting a success that
    did not happen.
    """
    actor.require(ROLE_REQUIRED_TO_BROADCAST)
    if record.recalled_at is not None:
        return "That broadcast has already been recalled."

    age = utcnow() - record.created_at
    if age > RECALL_WINDOW:
        raise DomainError(
            "This broadcast is more than 48 hours old. Telegram will not let a "
            "bot delete its own messages after that, so it cannot be recalled."
        )

    removed, stuck = 0, []
    for delivery in await deliveries_for(session, record):
        if delivery.telegram_message_id is None:
            continue
        try:
            await gateway.delete_message(
                delivery.telegram_chat_id, delivery.telegram_message_id
            )
            removed += 1
        except Exception as exc:
            logger.warning("Recall failed for %s: %s", delivery.chat_title, exc)
            stuck.append(delivery.chat_title or str(delivery.telegram_chat_id))

    record.recalled_at = utcnow()
    await session.flush()

    if stuck:
        return (
            f"Removed from {removed} groups. Still visible in: {', '.join(stuck)}. "
            f"Those will need deleting by hand."
        )
    return f"Recalled. Removed from {removed} groups."
