"""Full Phase 1 walkthrough against a fake Telegram. No token, no network.

Prints both sides of the conversation - what the client sees in their group,
and what staff see in the Operations topic - so the two can be compared. The
useful thing to look for is what is *absent* from the client column.

    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot.registry import (  # noqa: E402
    register_client_chat,
    register_operations_chat,
    upsert_staff,
)
from app.bot.routing import IncomingMessage, ReplyToAcknowledgementStrategy  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.domain.enums import Department, Priority, StaffRole, WorkItemStatus  # noqa: E402
from app.domain.errors import NotAuthorised  # noqa: E402
from app.domain.history import load_events, render_history  # noqa: E402
from app.domain.work_items import Actor  # noqa: E402
from app.services import relay  # noqa: E402
from app.services.gateway import FakeGateway  # noqa: E402
from app.services.relay import IncomingAttachment  # noqa: E402

CLIENT_CHAT = -1002000000001
OPS_CHAT = -1001000000001


def hr(title: str) -> None:
    print(f"\n{'═' * 72}\n  {title}\n{'═' * 72}")


def step(text: str) -> None:
    print(f"  → {text}")


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gw = FakeGateway()

    async with factory() as s:
        hr("SETUP")
        await register_operations_chat(
            s, telegram_chat_id=OPS_CHAT, department=Department.SUPPORT,
            title="Support Operations",
        )
        acme = await register_client_chat(
            s, telegram_chat_id=CLIENT_CHAT, client_name="Acme Payments",
            department=Department.SUPPORT, title="Acme — Support",
        )
        sarah = await upsert_staff(
            s, telegram_user_id=5001, display_name="Sarah Hill",
            role=StaffRole.OPERATOR, department=Department.SUPPORT,
        )
        james = await upsert_staff(
            s, telegram_user_id=5002, display_name="James Okoro",
            role=StaffRole.SENIOR_OPERATOR, department=Department.SUPPORT,
        )
        step("Support Operations group registered (topics enabled)")
        step("Acme Payments — Support group registered")
        step("Sarah Hill (operator), James Okoro (senior operator)")

        hr("1 · CLIENT RAISES A REQUEST")
        item = await relay.open_request(
            s, gw,
            source_chat=acme,
            subject="Settlement missing for 3 March",
            body="We have not received settlement for 3 March. Can you check?",
            raised_by_name="Tom Baker",
            raised_by_telegram_user_id=9001,
            attachments=[IncomingAttachment(
                file_id="BQACAgQAAx0", file_unique_id="u1", kind="document",
                file_name="march-statement.pdf", file_size=41_000_000,
            )],
        )
        step(f"{item.display_reference} created, topic {item.topic_id} opened")
        step("41 MB attachment relayed by file_id — never downloaded, so the "
             "20 MB cap does not apply")

        hr("2 · STAFF WORK IT")
        await relay.claim(s, gw, item, Actor.of(sarah))
        await relay.change_status(s, gw, item, WorkItemStatus.IN_PROGRESS, Actor.of(sarah))
        step("Sarah claimed and started")

        try:
            await relay.change_priority(s, gw, item, Priority.HIGH, Actor.of(sarah))
        except NotAuthorised as exc:
            step(f"Sarah tried to raise priority — refused ({exc})")
        await relay.change_priority(s, gw, item, Priority.HIGH, Actor.of(james))
        step("James raised it to High")

        await relay.add_internal_note(
            s, gw, item, Actor.of(james),
            "Acme are consistently late paying. Do not flag to the bank yet.",
        )
        step("James added an internal note (watch the client column below)")

        hr("3 · STAFF ASK THE CLIENT FOR SOMETHING")
        await relay.send_client_reply(
            s, gw, item, Actor.of(sarah),
            "could you send the payment confirmation from your side?",
        )
        await relay.change_status(s, gw, item, WorkItemStatus.WAITING_CLIENT, Actor.of(sarah))
        step("Sent, and status moved to Waiting for Client")

        hr("4 · CLIENT REPLIES TO THAT MESSAGE")
        anchor_id = gw.calls[-2].payload.get("message_id")  # not used; see below
        del anchor_id

        strategy = ReplyToAcknowledgementStrategy()
        # The client replies to the most recent thing we sent them.
        from sqlalchemy import select

        from app.db.models import Message as Msg
        from app.domain.enums import MessageDirection

        latest_outbound = (await s.execute(
            select(Msg).where(
                Msg.work_item_id == item.id,
                Msg.direction == MessageDirection.OUTBOUND,
            ).order_by(Msg.id.desc()).limit(1)
        )).scalar_one()

        resolved = await strategy.resolve(s, acme, IncomingMessage(
            telegram_chat_id=CLIENT_CHAT,
            telegram_message_id=9100,
            sender_name="Tom Baker",
            text="Attached — sent 3 March at 09:14.",
            reply_to_message_id=latest_outbound.telegram_message_id,
        ))
        step(f"Reply routed to {resolved.display_reference} with no staff effort")

        await relay.relay_client_message(
            s, gw, item,
            text="Attached — sent 3 March at 09:14.",
            sender_name="Tom Baker",
            telegram_message_id=9100,
            attachments=[IncomingAttachment(
                file_id="BQACAgQAAx1", file_unique_id="u2", kind="photo",
            )],
        )

        stray = await strategy.resolve(s, acme, IncomingMessage(
            telegram_chat_id=CLIENT_CHAT, telegram_message_id=9101,
            sender_name="Tom Baker", text="any news?", reply_to_message_id=None,
        ))
        step(f"A freshly typed message resolves to {stray} — open question with client")

        hr("5 · RESOLUTION")
        await relay.change_status(s, gw, item, WorkItemStatus.COMPLETED, Actor.of(sarah))
        await relay.close(s, gw, item, Actor.of(sarah))
        step(f"{item.display_reference} closed, topic archived")

        # ---- the two views ------------------------------------------------

        hr("WHAT THE CLIENT SAW  (their Telegram group)")
        for line in gw.messages_to(CLIENT_CHAT):
            print(f"  │ {line}")

        hr("WHAT STAFF SAW  (Support Operations topic)")
        for line in gw.messages_to(OPS_CHAT):
            for part in line.split("\n"):
                print(f"  │ {part}")

        hr("TOPIC HEADER, AS IT NOW READS  (edited in place, PRD 7.3)")
        for part in (gw.current_text(item.header_message_id) or "").split("\n"):
            print(f"  │ {part}")

        hr("AUDIT TRAIL")
        for line in render_history(await load_events(s, item)):
            print(f"  {line}")

        # ---- the check that matters ---------------------------------------
        hr("LEAK CHECK")
        client_text = gw.all_text_to(CLIENT_CHAT)
        for secret in ["consistently late", "Do not flag", "Internal note"]:
            status = "LEAKED" if secret.lower() in client_text.lower() else "contained"
            print(f"  {status:>10}  {secret!r}")

        print(f"\n  {len(await load_events(s, item))} events recorded. "
              f"Topic archived: {bool(gw.closed_topics)}\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
