"""The one place this project talks to a blockchain.

Implements `wallet.ChainClient` against TronGrid's public API, which is the
only reason any of this needs a network. Kept in its own small file so that
the thing which can fail for reasons outside this codebase - a rate limit, a
timeout, an API that changed shape - is somewhere you can see all of at once.

Uses aiohttp, which is already here because aiogram depends on it. Adding
httpx for one GET would be a dependency for politeness.

**Read-only.** One endpoint, one HTTP verb, no key and no signing. The same
boundary `wallet.py` holds, and for the same reason.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp

from app.services.wallet import IncomingPayment, from_micro_usdt

logger = logging.getLogger(__name__)

# USDT on Tron. The contract address is the asset: filtering on it is what
# stops every other token somebody sends to this address being read as a
# settlement.
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

TRONGRID = "https://api.trongrid.io"
TIMEOUT_SECONDS = 20

# TronGrid pages. Fifty is plenty for a fifteen-minute window on one address
# and keeps a restart after a long outage from pulling a year of history in
# one request.
PAGE_LIMIT = 50


class TronGridClient:
    """`wallet.ChainClient` over TronGrid.

    An API key is optional - the public tier works and is rate limited. It is
    read from settings rather than baked in so that moving to a keyed tier is
    a configuration change.
    """

    def __init__(self, api_key: str | None = None, base_url: str = TRONGRID):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def incoming_usdt(
        self, address: str, *, since: datetime | None = None
    ) -> list[IncomingPayment]:
        """TRC-20 USDT transfers *into* this address.

        `only_to` is what makes it incoming. Without it the same endpoint
        returns everything the address has sent as well, and a payment out
        would be read as a settlement arriving.

        An empty list on failure rather than an exception. This runs on a
        timer; a chain that is unreachable for ten minutes is a thing that
        happens, and the next pass picks up whatever was missed because the
        high-water mark is only moved when something is actually read.
        """
        params = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": USDT_CONTRACT,
            "limit": str(PAGE_LIMIT),
        }
        if since is not None:
            # TronGrid wants milliseconds. One second of overlap on purpose:
            # a payment landing in the same second as the last one read would
            # otherwise be skipped, and a duplicate is filtered by hash
            # upstream while a miss is money nobody knows arrived.
            params["min_timestamp"] = str(
                int(since.timestamp() * 1000) - 1000
            )

        url = f"{self._base_url}/v1/accounts/{address}/transactions/trc20"
        headers = {"TRON-PRO-API-KEY": self._api_key} if self._api_key else {}

        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(
                            "TronGrid returned %s for %s", response.status, address
                        )
                        return []
                    body = await response.json()
        except Exception:
            logger.exception("Could not reach TronGrid; will try again")
            return []

        return [
            payment
            for raw in (body.get("data") or [])
            if (payment := _as_payment(raw, address)) is not None
        ]


def _as_payment(raw: dict, address: str) -> IncomingPayment | None:
    """One TronGrid row, or None if it is not a payment to us.

    Checked rather than trusted. `only_to` should make the destination check
    redundant, and it is done anyway: this decides whether somebody is told
    their money arrived, and a changed API default is not a thing to find out
    from a client.
    """
    try:
        if (raw.get("to") or "") != address:
            return None
        if (raw.get("type") or "Transfer") != "Transfer":
            return None
        at = datetime.fromtimestamp(int(raw["block_timestamp"]) / 1000, tz=UTC)
        return IncomingPayment(
            tx_hash=str(raw["transaction_id"]).lower(),
            amount_usdt=from_micro_usdt(raw["value"]),
            at=at,
            from_address=raw.get("from"),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Unreadable TronGrid row: %s", raw)
        return None


__all__ = ["PAGE_LIMIT", "TRONGRID", "USDT_CONTRACT", "TronGridClient"]
