"""Extracting file references from a Telegram message.

We keep the `file_id` and never the bytes. Relaying by reference has no size
limit; downloading would cap us at 20 MB.
"""

from __future__ import annotations

from aiogram.types import Message

from app.services.relay import IncomingAttachment

_SIMPLE_KINDS = (
    ("document", "document"),
    ("video", "video"),
    ("audio", "audio"),
    ("voice", "voice"),
    ("animation", "animation"),
    ("video_note", "video_note"),
)


def extract_attachments(message: Message) -> list[IncomingAttachment]:
    out: list[IncomingAttachment] = []

    if message.photo:
        # Telegram sends several sizes; the last is the largest.
        largest = message.photo[-1]
        out.append(
            IncomingAttachment(
                file_id=largest.file_id,
                file_unique_id=largest.file_unique_id,
                kind="photo",
                file_size=largest.file_size,
            )
        )

    for attr, kind in _SIMPLE_KINDS:
        obj = getattr(message, attr, None)
        if obj is None:
            continue
        out.append(
            IncomingAttachment(
                file_id=obj.file_id,
                file_unique_id=obj.file_unique_id,
                kind=kind,
                file_name=getattr(obj, "file_name", None),
                mime_type=getattr(obj, "mime_type", None),
                file_size=getattr(obj, "file_size", None),
            )
        )

    return out


def has_attachment(message: Message) -> bool:
    return bool(extract_attachments(message))
