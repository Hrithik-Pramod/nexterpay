"""Application settings, loaded from environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://nexterpay:nexterpay@localhost:5432/nexterpay_ops"
    redis_url: str = "redis://localhost:6379/0"

    # Reply routing. "reply_to_ack" is the mechanism agreed with NexterPay:
    # the bot posts an acknowledgement carrying the reference, and clients
    # reply to it. "most_recent" is retained as a fallback strategy only.
    reply_routing_strategy: str = "reply_to_ack"

    # Outbound relay is explicit by design - a staff message only reaches the
    # client via /reply. Do not flip this to make plain messages relay.
    outbound_command: str = "reply"

    # Telegram user id treated as administrator until a real one exists in the
    # database. Needed because the first administrator cannot add themselves.
    admin_bootstrap_id: int | None = None

    # Whether closing a work item tells the client. Open question A2 with
    # NexterPay; defaulting to on because silence after a request is worse.
    notify_client_on_close: bool = True

    log_level: str = "INFO"
    debug_sql: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
