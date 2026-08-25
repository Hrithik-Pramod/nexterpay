"""Persistence model.

Design notes
------------
* `Event` is append-only. Nothing in this application may update or delete a
  row in `events`. It is the audit trail required by PRD sections 3.7 and 7,
  and it is also what renders the visible status messages inside a Telegram
  topic (see `app.domain.history.render_event`).

* `Chat` maps a Telegram chat id onto a client and department. An unregistered
  chat is ignored entirely - the bot never acts in a group it does not know.

* `Message.telegram_message_id` is indexed alongside `chat_id` because the
  reply-to-acknowledgement routing strategy resolves an inbound reply by
  looking up the message it replied to.

* Staff are soft-deactivated rather than deleted, so historical events keep
  resolving to a name.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow
from app.domain.enums import (
    ChatKind,
    Department,
    EventType,
    MessageDirection,
    Priority,
    StaffRole,
    WorkItemStatus,
)


def _enum(py_enum, name: str):
    return SAEnum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class Client(Base, TimestampMixin):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    chats: Mapped[list[Chat]] = relationship(back_populates="client")

    def __repr__(self) -> str:
        return f"<Client {self.name!r}>"


class Chat(Base, TimestampMixin):
    """A registered Telegram group.

    Client groups carry a client_id. Operations Groups do not - they belong to
    NexterPay rather than to any one client.
    """

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    kind: Mapped[ChatKind] = mapped_column(_enum(ChatKind, "chat_kind"), nullable=False)
    department: Mapped[Department] = mapped_column(_enum(Department, "department"), nullable=False)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    client: Mapped[Client | None] = relationship(back_populates="chats")

    __table_args__ = (
        # At most one Operations Group per department.
        Index(
            "uq_operations_chat_per_department",
            "department",
            unique=True,
            sqlite_where=(kind == ChatKind.OPERATIONS),
            postgresql_where=(kind == ChatKind.OPERATIONS),
        ),
    )

    def __repr__(self) -> str:
        return f"<Chat {self.kind.value}/{self.department.value} tg={self.telegram_chat_id}>"


class Staff(Base, TimestampMixin):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[StaffRole] = mapped_column(_enum(StaffRole, "staff_role"), nullable=False)
    department: Mapped[Department] = mapped_column(_enum(Department, "department"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Staff {self.display_name!r} {self.role.value}>"


class ReferenceCounter(Base):
    """Single-row table backing the human-facing work item reference number.

    Kept separate from the work_items primary key so references stay stable and
    readable (#1042) regardless of any future data migration.
    """

    __tablename__ = "reference_counter"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)


class WorkItem(Base, TimestampMixin):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    department: Mapped[Department] = mapped_column(_enum(Department, "department"), nullable=False)

    source_chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), nullable=False)
    operations_chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"), nullable=False)
    topic_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The pinned summary at the top of the topic. Kept so it can be edited in
    # place as ownership, status and priority change - PRD 7.3 requires
    # ownership to be clearly visible, and a stale header is not that.
    header_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    raised_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raised_by_name: Mapped[str] = mapped_column(String(200), nullable=False)

    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    original_message: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[WorkItemStatus] = mapped_column(
        _enum(WorkItemStatus, "work_item_status"), nullable=False, default=WorkItemStatus.OPEN
    )
    priority: Mapped[Priority] = mapped_column(
        _enum(Priority, "priority"), nullable=False, default=Priority.MEDIUM
    )
    owner_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship()
    owner: Mapped[Staff | None] = relationship()
    source_chat: Mapped[Chat] = relationship(foreign_keys=[source_chat_id])
    operations_chat: Mapped[Chat] = relationship(foreign_keys=[operations_chat_id])
    events: Mapped[list[Event]] = relationship(
        back_populates="work_item", order_by="Event.id", cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(
        back_populates="work_item", order_by="Message.id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_work_items_open_by_source", "source_chat_id", "status"),
    )

    @property
    def display_reference(self) -> str:
        return f"#{self.reference}"

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal

    def __repr__(self) -> str:
        return f"<WorkItem {self.display_reference} {self.status.value}>"


class Message(Base):
    """Every message that passes through the platform, in either direction.

    Retained even if the original is later deleted in Telegram - the Bot API
    does not notify us of deletions, so our copy outlives the client's.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        _enum(MessageDirection, "message_direction"), nullable=False
    )

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Nullable because some records have no Telegram message behind them - an
    # internal note recorded outside a topic, for example. NULLs do not collide
    # under the unique constraint below, which is the point.
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sender_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    work_item: Mapped[WorkItem] = relationship(back_populates="messages")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("telegram_chat_id", "telegram_message_id", name="uq_message_identity"),
        # Reply-to-acknowledgement routing looks up by this pair.
        Index("ix_messages_lookup", "telegram_chat_id", "telegram_message_id"),
    )


class Attachment(Base):
    """Client and staff attachments.

    `file_id` is sufficient to relay a file to another chat without downloading
    it, which sidesteps the Bot API's 20 MB download ceiling. `stored_path` is
    populated only if NexterPay elects to retain its own copy.
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), nullable=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False)

    file_id: Mapped[str] = mapped_column(String(300), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(100), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # photo, document, video...
    stored_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="attachments")


class Event(Base):
    """Append-only audit record. Never updated, never deleted."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"), nullable=False)
    event_type: Mapped[EventType] = mapped_column(_enum(EventType, "event_type"), nullable=False)

    actor_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    actor_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(200), nullable=False, default="System")

    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    work_item: Mapped[WorkItem] = relationship(back_populates="events")

    __table_args__ = (Index("ix_events_work_item_seq", "work_item_id", "id"),)

    def __repr__(self) -> str:
        return f"<Event {self.event_type.value} wi={self.work_item_id}>"
