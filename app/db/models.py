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
    CheckConstraint,
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
    """A counterparty - a client or a supplier.

    One table for both, because NexterPay confirmed a supplier request is the
    same process with different labels. A supplier is not a different kind of
    thing here, only a different role in a particular request: the same
    organisation can be a client on one ticket and the supplier on another.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)

    # The four-letter code that leads every reference and topic title, and the
    # thing you type into Telegram search to find everything for this
    # counterparty. Nullable because counterparties registered before codes
    # existed have none until one is assigned.
    code: Mapped[str | None] = mapped_column(String(4), nullable=True, unique=True, index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    chats: Mapped[list[Chat]] = relationship(back_populates="client")

    def __repr__(self) -> str:
        return f"<Client {self.code or '????'} {self.name!r}>"


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

    # Whether this counterparty group is a supplier rather than a client.
    #
    # A flag rather than a third ChatKind on purpose. Suppliers behave exactly
    # like clients - NexterPay confirmed it is the same process with different
    # labels - so making them a separate kind would mean revisiting every
    # permission and routing decision that currently turns on ChatKind.CLIENT.
    # The only thing that genuinely needs to tell them apart is broadcasting,
    # which targets clients only or suppliers only.
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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


class Broadcast(Base, TimestampMixin):
    """One message sent to many counterparty groups at once.

    Recorded in full because a broadcast is the highest-reach action on the
    platform: it needs to be answerable later who sent what, to whom, and
    whether it actually arrived. The per-recipient rows also make the recall
    possible - Telegram will delete a bot's own message for 48 hours, but only
    if you know which message in which chat.
    """

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    sent_by_name: Mapped[str] = mapped_column(String(200), nullable=False)
    audience: Mapped[str] = mapped_column(String(60), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    deliveries: Mapped[list[BroadcastDelivery]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Broadcast {self.id} {self.audience!r}>"


class BroadcastDelivery(Base):
    """One broadcast, one group. Records failures rather than hiding them."""

    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id"), nullable=False, index=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Populated when a send fails - a bot removed from a group fails quietly
    # otherwise, and "it went to everyone" would be a lie.
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)

    broadcast: Mapped[Broadcast] = relationship(back_populates="deliveries")


class Staff(Base, TimestampMixin):
    """A person. Which desks they work is held separately, in `memberships`.

    Originally a person had one department and one role, both columns here.
    NexterPay confirmed during testing that they have people who genuinely
    span two, so seniority is now a fact about a person *in a department*
    rather than about the person: someone can be a Manager on their own desk
    and an Operator on the one they help out with. Collapsing that back to a
    single role would mean either over-promoting them somewhere or refusing
    them where they belong.
    """

    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Eager by default. Every permission check reads this, and a lazy load
    # inside async code raises MissingGreenlet - a failure this project has
    # already been bitten by once.
    # Eager by default. Every permission check reads this, and a lazy load
    # inside async code raises MissingGreenlet - a failure this project has
    # already been bitten by once.
    #
    # Deliberately NOT ordered in SQL. `ORDER BY department` does two
    # different things: on SQLite the column is text and sorts alphabetically,
    # on Postgres it is a real enum and sorts by the order the values were
    # declared. So the tests saw one order and the live server another, and
    # the tests were the ones that were wrong. Ordering happens in `desks`
    # instead, where both databases get the same answer.
    memberships: Mapped[list[StaffDepartment]] = relationship(
        back_populates="staff",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def desks(self) -> list[StaffDepartment]:
        """Their departments, in a fixed order, for anything a person reads."""
        return sorted(self.memberships, key=lambda m: m.department.label)

    def role_in(self, department: Department) -> StaffRole | None:
        """Their seniority on that desk, or None if they do not work it."""
        for membership in self.memberships:
            if membership.department is department:
                return membership.role
        return None

    @property
    def departments(self) -> list[Department]:
        return [m.department for m in self.desks]

    @property
    def is_administrator(self) -> bool:
        """Administrator anywhere means administrator everywhere.

        Registering groups and adding people are not departmental acts, and
        an administrator who had to be added to all five desks before they
        could configure them would be a worse arrangement than the one this
        replaced.
        """
        return any(m.role is StaffRole.ADMINISTRATOR for m in self.memberships)

    def __repr__(self) -> str:
        desks = ", ".join(f"{m.department.value}:{m.role.value}" for m in self.memberships)
        return f"<Staff {self.display_name!r} {desks or 'no departments'}>"


class StaffDepartment(Base, TimestampMixin):
    """One person on one desk, at one level of seniority.

    A person with no rows here is registered but works nowhere, which is
    treated exactly as not being staff. That is deliberate: removing someone
    from their last department should not silently leave them with access.
    """

    __tablename__ = "staff_departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    department: Mapped[Department] = mapped_column(
        _enum(Department, "department"), nullable=False
    )
    role: Mapped[StaffRole] = mapped_column(_enum(StaffRole, "staff_role"), nullable=False)

    staff: Mapped[Staff] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("staff_id", "department", name="uq_staff_department"),
        Index("ix_staff_departments_department", "department"),
    )

    def __repr__(self) -> str:
        return f"<StaffDepartment {self.department.value} {self.role.value}>"


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

    # Set after the fact by staff, using the File under button. A client
    # raising a request does not know which supplier it concerns, and often
    # nobody does until someone has looked at it.
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)

    # The codes are copied onto the work item rather than read through the
    # relationships. Two reasons. Reading them live would mean lazy-loading
    # inside async code, which is the error this project has already been
    # bitten by. And a reference that is already in circulation - written in
    # an email, quoted on a call - should not silently change because someone
    # later edited a counterparty's code.
    client_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    supplier_code: Mapped[str | None] = mapped_column(String(4), nullable=True)

    raised_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raised_by_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Which way round this one went. Without it, "issues we raised with this
    # supplier" and "issues this client raised with us" are indistinguishable
    # once they are both sitting in the same topic list, and NexterPay need to
    # tell them apart for the integration work.
    raised_by_us: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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

    # Both point at `clients`, so the join has to be spelled out.
    client: Mapped[Client] = relationship(foreign_keys=[client_id])
    supplier: Mapped[Client | None] = relationship(foreign_keys=[supplier_id])
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
        """The internal reference. Carries the supplier code where there is one."""
        if self.client_code and self.supplier_code:
            return f"{self.client_code}-{self.supplier_code}-{self.reference}"
        if self.client_code:
            return f"{self.client_code}-{self.reference}"
        # Raised before codes existed. Left alone rather than renumbered.
        return f"#{self.reference}"

    @property
    def client_reference(self) -> str:
        """What the client is shown. Never carries the supplier code.

        A client who can see which supplier their issue was filed against can
        work out who NexterPay use for what, and that is not always something
        NexterPay would choose to disclose.
        """
        if self.client_code:
            return f"{self.client_code}-{self.reference}"
        return f"#{self.reference}"

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal

    def __repr__(self) -> str:
        return f"<WorkItem {self.display_reference} {self.status.value}>"


class WorkItemLink(Base, TimestampMixin):
    """Two tickets that concern the same underlying problem.

    A link is symmetric: whichever one you are reading, the other is visible.
    That is enforced by the storage rather than by the code - the pair is
    always written with the lower id first, so "A is linked to B" and "B is
    linked to A" are the same row. Without that ordering, linking twice in
    opposite directions would produce two rows and the other ticket would
    appear in the list twice, which is the sort of thing nobody notices until
    a client is looking at it.

    The check constraint makes a ticket linking to itself impossible rather
    than merely refused, and the unique constraint makes a duplicate
    impossible for the same reason. Both are also checked in the service layer
    so the person gets a sentence rather than a database error, but the
    constraints are what makes it true.

    Deliberately flat. NexterPay were offered a parent-and-child hierarchy and
    it was rejected: hierarchies require everyone to agree which ticket is the
    senior one, and that argument is not worth having.
    """

    __tablename__ = "work_item_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    lower_work_item_id: Mapped[int] = mapped_column(
        ForeignKey("work_items.id"), nullable=False
    )
    higher_work_item_id: Mapped[int] = mapped_column(
        ForeignKey("work_items.id"), nullable=False
    )

    # Kept even after someone leaves, like every other actor name on the
    # platform, so the trail still resolves to a person.
    created_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id"), nullable=True
    )
    created_by_name: Mapped[str] = mapped_column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "lower_work_item_id", "higher_work_item_id", name="uq_work_item_link"
        ),
        CheckConstraint(
            "lower_work_item_id < higher_work_item_id", name="ck_work_item_link_order"
        ),
        # Lookups go both ways: "what is linked to this one" has to find rows
        # where this ticket is on either side.
        Index("ix_work_item_links_lower", "lower_work_item_id"),
        Index("ix_work_item_links_higher", "higher_work_item_id"),
    )

    def __repr__(self) -> str:
        return f"<WorkItemLink {self.lower_work_item_id}<->{self.higher_work_item_id}>"


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
