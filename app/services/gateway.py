"""Everything this application does to Telegram goes through here.

One boundary, for two reasons. The relay can be tested exhaustively against
`FakeGateway` without a token or a network, and the rate limiter in
`app.services.throttle` has a single place to wrap.

No module outside `app/services` and `app/bot` should import aiogram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentMessage:
    """What we need back after sending: the id, so a client reply that points
    at this message can be resolved to its work item."""

    chat_id: int
    message_id: int


@runtime_checkable
class TelegramGateway(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: Any | None = None,
        # Off by default on purpose. Most of what this bot sends is text a
        # client or staff member typed, and a stray "<" in it would either be
        # swallowed as markup or rejected outright by Telegram. Only callers
        # that build their own markup - and escape everything they interpolate
        # - should turn this on.
        parse_mode: str | None = None,
    ) -> SentMessage: ...

    async def send_file(
        self,
        chat_id: int,
        file_id: str,
        kind: str,
        *,
        thread_id: int | None = None,
        caption: str | None = None,
    ) -> SentMessage:
        """Relay a file by `file_id`.

        Deliberately never downloads. The Bot API caps downloads at 20 MB but
        places no such limit on re-sending a file we already have an id for, so
        attachments of any size pass through.
        """
        ...

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: Any | None = None
    ) -> None: ...

    async def create_topic(self, chat_id: int, name: str) -> int: ...

    async def close_topic(self, chat_id: int, thread_id: int) -> None: ...

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any | None
    ) -> None: ...


class AiogramGateway:
    """Production implementation."""

    def __init__(self, bot) -> None:  # aiogram.Bot
        self._bot = bot

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        msg = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return SentMessage(chat_id=chat_id, message_id=msg.message_id)

    async def send_file(
        self,
        chat_id: int,
        file_id: str,
        kind: str,
        *,
        thread_id: int | None = None,
        caption: str | None = None,
    ) -> SentMessage:
        senders = {
            "photo": self._bot.send_photo,
            "document": self._bot.send_document,
            "video": self._bot.send_video,
            "audio": self._bot.send_audio,
            "voice": self._bot.send_voice,
            "animation": self._bot.send_animation,
        }
        send = senders.get(kind, self._bot.send_document)
        param = kind if kind in senders else "document"
        msg = await send(
            chat_id=chat_id,
            **{param: file_id},
            message_thread_id=thread_id,
            caption=caption,
        )
        return SentMessage(chat_id=chat_id, message_id=msg.message_id)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: Any | None = None
    ) -> None:
        await self._bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
        )

    async def create_topic(self, chat_id: int, name: str) -> int:
        topic = await self._bot.create_forum_topic(chat_id=chat_id, name=name[:128])
        return topic.message_thread_id

    async def close_topic(self, chat_id: int, thread_id: int) -> None:
        try:
            await self._bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        except TelegramBadRequest as exc:
            # A topic somebody already closed by hand in Telegram is not an
            # error - the desired state is the current state. Anything else
            # still raises.
            if "TOPIC_NOT_MODIFIED" not in str(exc):
                raise
            logger.info("Topic %s in %s was already closed", thread_id, chat_id)

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any | None
    ) -> None:
        await self._bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )


@dataclass
class Call:
    method: str
    chat_id: int
    payload: dict


class FakeGateway:
    """Test double. Records everything; invents plausible message ids.

    `messages_to(chat_id)` is what the leak tests assert on - if an internal
    note ever reaches a client chat, it shows up there.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.topics: dict[int, list[int]] = {}
        self.closed_topics: list[tuple[int, int]] = []
        self.edits: dict[int, list[str]] = {}
        self._next_message_id = 1000
        self._next_topic_id = 500
        self.fail_next: Exception | None = None

    def _id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        self._maybe_fail()
        self.calls.append(
            Call("send_message", chat_id, {
                "text": text,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
                "has_markup": reply_markup is not None,
                "parse_mode": parse_mode,
            })
        )
        return SentMessage(chat_id=chat_id, message_id=self._id())

    async def send_file(
        self,
        chat_id: int,
        file_id: str,
        kind: str,
        *,
        thread_id: int | None = None,
        caption: str | None = None,
    ) -> SentMessage:
        self._maybe_fail()
        self.calls.append(
            Call("send_file", chat_id, {
                "file_id": file_id, "kind": kind,
                "thread_id": thread_id, "caption": caption,
            })
        )
        return SentMessage(chat_id=chat_id, message_id=self._id())

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: Any | None = None
    ) -> None:
        self._maybe_fail()
        self.edits.setdefault(message_id, []).append(text)
        self.calls.append(
            Call("edit_message_text", chat_id, {"message_id": message_id, "text": text})
        )

    async def create_topic(self, chat_id: int, name: str) -> int:
        self._maybe_fail()
        self._next_topic_id += 1
        self.topics.setdefault(chat_id, []).append(self._next_topic_id)
        self.calls.append(Call("create_topic", chat_id, {"name": name}))
        return self._next_topic_id

    async def close_topic(self, chat_id: int, thread_id: int) -> None:
        self._maybe_fail()
        self.closed_topics.append((chat_id, thread_id))
        self.calls.append(Call("close_topic", chat_id, {"thread_id": thread_id}))

    async def edit_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any | None
    ) -> None:
        self.calls.append(
            Call("edit_reply_markup", chat_id, {"message_id": message_id})
        )

    # -- assertions helpers -------------------------------------------------

    def messages_to(self, chat_id: int) -> list[str]:
        return [
            c.payload.get("text", "")
            for c in self.calls
            if c.chat_id == chat_id and c.method == "send_message"
        ]

    def files_to(self, chat_id: int) -> list[str]:
        return [
            c.payload["file_id"]
            for c in self.calls
            if c.chat_id == chat_id and c.method == "send_file"
        ]

    def all_text_to(self, chat_id: int) -> str:
        return "\n".join(self.messages_to(chat_id))

    def current_text(self, message_id: int) -> str | None:
        """Latest version of a message, after any edits."""
        edits = self.edits.get(message_id)
        if edits:
            return edits[-1]
        for call in self.calls:
            if call.method == "send_message" and call.payload.get("_id") == message_id:
                return call.payload["text"]
        return None
