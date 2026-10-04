"""Telling the desk when money arrives.

The loop that joins `wallet.py` to the Operations Group. Everything that
decides anything lives in `wallet.py` and is tested without a network; this
file fetches, looks up, and posts.

**It proposes. It never records.** A payment that matches one open deal
exactly is still only a proposal, because the thing it is proposing is that a
client's money has arrived - and the platform has one fact (an amount) where
a person has several (who was expected to pay today, what the supplier said
this morning, which deal has been chased twice). The desk confirms with
`/npsettle` as they do now, with the figures already in front of them.

Seen payments are remembered so a restart does not re-announce a week of
them. A transaction hash is the natural key for that: it is unique, it is
what the chain gives us, and it is what the desk will quote back.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.registry import get_setting, set_setting
from app.db.models import Chat, FxOrder, Settlement
from app.domain.enums import ChatKind, Department, FxOrderStatus
from app.services import wallet

logger = logging.getLogger(__name__)

# The most recent payment already announced. Stored rather than inferred,
# because "have we mentioned this one" cannot be answered from the orders -
# an unmatched payment leaves no trace on any of them, and those are exactly
# the ones worth announcing twice as little as any other.
SEEN_SETTING = "fx.last_seen_payment_at"


async def _already_recorded(session: AsyncSession, tx_hash: str) -> bool:
    """Has the desk already settled this payment themselves?

    They will often beat the watcher to it - the supplier sends the hash in
    the group, somebody pastes it into `/npsettle`, and the chain poll
    arrives afterwards. Announcing it then would be the platform telling the
    desk about something they did.
    """
    result = await session.execute(
        select(Settlement).where(Settlement.tx_hash == tx_hash.lower())
    )
    return result.scalars().first() is not None


async def _open_deals(session: AsyncSession) -> list[FxOrder]:
    result = await session.execute(
        select(FxOrder)
        .where(FxOrder.status == FxOrderStatus.AWAITING_SETTLEMENT)
        .order_by(FxOrder.reference)
    )
    return list(result.scalars().all())


async def _finance_operations(session: AsyncSession) -> Chat | None:
    """Where to say it.

    The Finance desk's Operations Group, because that is whose book this is.
    None rather than a guess if it does not exist - posting a payment
    notification into whichever operations group happens to be first would
    put settlement figures in front of a desk that has no business with them.
    """
    result = await session.execute(
        select(Chat).where(
            Chat.kind == ChatKind.OPERATIONS,
            Chat.department == Department.FINANCE,
        )
    )
    return result.scalars().first()


def describe(proposal: wallet.Proposal) -> str:
    """One payment, as the desk needs to read it."""
    amount = f"{proposal.payment.amount_usdt:,f}".rstrip("0").rstrip(".")
    head = f"💰 <b>{amount} USDT arrived</b>"

    if proposal.is_certain:
        candidate = proposal.candidates[0]
        difference = proposal.difference()
        line = f"Looks like {candidate.key}."
        if difference and abs(difference) >= wallet.MATCH_FLOOR / 10:
            direction = "short" if difference < 0 else "over"
            line += f" That is {abs(difference):,f} USDT {direction} of it."
        return (
            f"{head}\n\n{line}\n\n"
            f"<i>Nothing recorded — settle it with /npsettle when you have "
            f"the supplier's lines.</i>"
        )

    if proposal.is_ambiguous:
        names = ", ".join(c.key for c in proposal.candidates)
        return (
            f"{head}\n\nIt could be any of {names}, so I have not guessed.\n\n"
            f"<i>Settle the right one with /npsettle.</i>"
        )

    return (
        f"{head}\n\nNo open deal is waiting on that amount.\n\n"
        f"<i>Either a supplier has paid early, or this is money nobody has "
        f"accounted for.</i>"
    )


async def poll(
    session: AsyncSession,
    gateway,
    client: wallet.ChainClient,
    *,
    now: datetime | None = None,
) -> int:
    """One pass. Returns how many payments were announced.

    Returns rather than raises on the ordinary empty cases - no wallet set,
    no Finance group, nothing new - because this runs on a timer and a loop
    that logs an exception every fifteen minutes for a configuration nobody
    has set yet is a loop whose logs stop being read.
    """
    address = await get_setting(session, wallet.WALLET_SETTING)
    if not address:
        return 0

    ops = await _finance_operations(session)
    if ops is None:
        logger.warning("A wallet is being watched but no Finance Operations "
                       "Group exists to tell about it")
        return 0

    since_raw = await get_setting(session, SEEN_SETTING)
    since = datetime.fromisoformat(since_raw) if since_raw else None

    payments = await client.incoming_usdt(address, since=since)
    if not payments:
        return 0

    fresh = [
        payment for payment in payments
        if not await _already_recorded(session, payment.tx_hash)
        and (since is None or payment.at > since)
    ]
    if not fresh:
        return 0

    candidates = wallet.candidates_from_orders(await _open_deals(session))
    announced = 0

    for proposal in wallet.match_all(fresh, candidates):
        await gateway.send_message(
            ops.telegram_chat_id, describe(proposal), parse_mode="HTML"
        )
        announced += 1

    # Written after the sending, so a failure mid-pass means the unsent ones
    # are seen again next time. A payment announced twice is a nuisance; one
    # never announced is money nobody knows arrived.
    latest = max(payment.at for payment in fresh)
    await set_setting(session, SEEN_SETTING, latest.isoformat(), by="System")
    return announced


__all__ = ["SEEN_SETTING", "describe", "poll"]
