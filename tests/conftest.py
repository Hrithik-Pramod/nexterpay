"""Test fixtures.

Tests run against in-memory SQLite. The domain layer has no Telegram
dependency, so the whole lifecycle is testable without a bot or a token.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.registry import register_client_chat, register_operations_chat, upsert_staff
from app.db.base import Base
from app.db.models import Chat, Client, Staff  # noqa: F401  (register mappers)
from app.domain.enums import Department, StaffRole


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def support_ops(session: AsyncSession) -> Chat:
    return await register_operations_chat(
        session,
        telegram_chat_id=-1001000000001,
        department=Department.SUPPORT,
        title="Support Operations",
    )


@pytest_asyncio.fixture
async def acme_support(session: AsyncSession, support_ops: Chat) -> Chat:
    return await register_client_chat(
        session,
        telegram_chat_id=-1002000000001,
        client_name="Acme Payments",
        department=Department.SUPPORT,
        title="Acme — Support",
    )


@pytest_asyncio.fixture
async def operator(session: AsyncSession) -> Staff:
    return await upsert_staff(
        session,
        telegram_user_id=5001,
        display_name="Sarah Hill",
        role=StaffRole.OPERATOR,
        department=Department.SUPPORT,
    )


@pytest_asyncio.fixture
async def senior(session: AsyncSession) -> Staff:
    return await upsert_staff(
        session,
        telegram_user_id=5002,
        display_name="James Okoro",
        role=StaffRole.SENIOR_OPERATOR,
        department=Department.SUPPORT,
    )


@pytest_asyncio.fixture
async def manager(session: AsyncSession) -> Staff:
    return await upsert_staff(
        session,
        telegram_user_id=5003,
        display_name="Priya Nair",
        role=StaffRole.MANAGER,
        department=Department.SUPPORT,
    )


@pytest_asyncio.fixture
async def pexi_supplier(session: AsyncSession, support_ops: Chat) -> Chat:
    """A supplier group, so broadcast targeting has something to tell apart."""
    return await register_client_chat(
        session,
        telegram_chat_id=-1003000000001,
        client_name="Supplier Pexi",
        department=Department.SUPPORT,
        title="Pexi — Support",
        is_supplier=True,
    )
