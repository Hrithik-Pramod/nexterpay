"""Rate limiting.

Telegram's documented ceilings: roughly one message per second into any one
chat, no more than twenty per minute into a group, and about thirty per second
overall. Exceeding them returns 429 with a `retry_after`.

The Operations Groups are the constraint, not the client groups - every client
message produces at least one further send into a single Operations Group, so
that group is where a busy morning would hit the twenty-per-minute wall.

This is an in-process limiter, which is correct while there is exactly one bot
instance (a hard requirement under long polling anyway). If the deployment ever
grows a second process, this needs to move behind Redis.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)

PER_CHAT_INTERVAL = 1.05          # seconds between sends to one chat
GROUP_PER_MINUTE = 20
GLOBAL_PER_SECOND = 25            # under the ~30 ceiling, deliberately
MAX_RETRIES = 3


class RateLimiter:
    def __init__(
        self,
        per_chat_interval: float = PER_CHAT_INTERVAL,
        group_per_minute: int = GROUP_PER_MINUTE,
        global_per_second: int = GLOBAL_PER_SECOND,
        *,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.per_chat_interval = per_chat_interval
        self.group_per_minute = group_per_minute
        self.global_per_second = global_per_second
        self._clock = clock
        self._sleep = sleep

        self._last_send: dict[int, float] = {}
        self._minute_window: dict[int, deque[float]] = defaultdict(deque)
        self._second_window: deque[float] = deque()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_lock = asyncio.Lock()

    async def acquire(self, chat_id: int) -> float:
        """Block until it is safe to send to `chat_id`. Returns seconds waited."""
        waited = 0.0
        async with self._locks[chat_id]:
            now = self._clock()

            last = self._last_send.get(chat_id)
            if last is not None:
                gap = now - last
                if gap < self.per_chat_interval:
                    delay = self.per_chat_interval - gap
                    await self._sleep(delay)
                    waited += delay
                    now = self._clock()

            window = self._minute_window[chat_id]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.group_per_minute:
                delay = 60 - (now - window[0]) + 0.05
                logger.warning(
                    "Chat %s at the 20/minute ceiling; holding %.1fs", chat_id, delay
                )
                await self._sleep(delay)
                waited += delay
                now = self._clock()
                while window and now - window[0] > 60:
                    window.popleft()

            async with self._global_lock:
                while self._second_window and now - self._second_window[0] > 1:
                    self._second_window.popleft()
                if len(self._second_window) >= self.global_per_second:
                    delay = 1 - (now - self._second_window[0]) + 0.01
                    await self._sleep(delay)
                    waited += delay
                    now = self._clock()
                    while self._second_window and now - self._second_window[0] > 1:
                        self._second_window.popleft()
                self._second_window.append(now)

            self._last_send[chat_id] = now
            window.append(now)

        return waited


def _retry_after(exc: Exception) -> float | None:
    """Pull `retry_after` off a Telegram 429 without importing aiogram here."""
    for attr in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


class ThrottledGateway:
    """Wraps a gateway so every send passes the limiter first.

    Also honours `retry_after` if Telegram pushes back anyway - the limiter is
    conservative, but bursts from several clients at once can still trip it.
    """

    def __init__(self, inner, limiter: RateLimiter | None = None, *, sleep=asyncio.sleep) -> None:
        self._inner = inner
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep

    async def _guarded(self, chat_id: int, func, *args, **kwargs):
        for attempt in range(1, MAX_RETRIES + 1):
            await self._limiter.acquire(chat_id)
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                delay = _retry_after(exc)
                if delay is None or attempt == MAX_RETRIES:
                    raise
                logger.warning(
                    "429 from Telegram on chat %s; retrying in %.1fs (attempt %d)",
                    chat_id, delay, attempt,
                )
                await self._sleep(delay)
        raise RuntimeError("unreachable")

    async def send_message(self, chat_id: int, text: str, **kwargs):
        return await self._guarded(
            chat_id, self._inner.send_message, chat_id, text, **kwargs
        )

    async def send_file(self, chat_id: int, file_id: str, kind: str, **kwargs):
        return await self._guarded(
            chat_id, self._inner.send_file, chat_id, file_id, kind, **kwargs
        )

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs):
        return await self._guarded(
            chat_id, self._inner.edit_message_text, chat_id, message_id, text, **kwargs
        )

    async def create_topic(self, chat_id: int, name: str) -> int:
        return await self._guarded(chat_id, self._inner.create_topic, chat_id, name)

    async def rename_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        return await self._guarded(
            chat_id, self._inner.rename_topic, chat_id, thread_id, name
        )

    async def close_topic(self, chat_id: int, thread_id: int) -> None:
        return await self._guarded(
            chat_id, self._inner.close_topic, chat_id, thread_id
        )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        return await self._guarded(
            chat_id, self._inner.delete_message, chat_id, message_id
        )

    async def reopen_topic(self, chat_id: int, thread_id: int) -> None:
        return await self._guarded(
            chat_id, self._inner.reopen_topic, chat_id, thread_id
        )

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any | None
    ) -> None:
        return await self._guarded(
            chat_id, self._inner.edit_reply_markup, chat_id, message_id, reply_markup
        )
