"""Rate limiting.

Uses a fake clock so the tests assert on how long the limiter *would* have
waited, without actually waiting. Testing this against wall time would make the
suite slow and flaky, and would still not prove the arithmetic.
"""

from __future__ import annotations

import pytest

from app.services.gateway import FakeGateway
from app.services.throttle import RateLimiter, ThrottledGateway


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> RateLimiter:
    return RateLimiter(clock=clock, sleep=clock.sleep)


async def test_first_send_is_immediate(limiter, clock):
    assert await limiter.acquire(-100) == 0
    assert clock.total_slept == 0


async def test_second_send_to_same_chat_waits_a_second(limiter, clock):
    await limiter.acquire(-100)
    waited = await limiter.acquire(-100)

    assert waited == pytest.approx(1.05, abs=0.01)


async def test_no_wait_if_enough_time_has_passed(limiter, clock):
    await limiter.acquire(-100)
    clock.advance(2.0)

    assert await limiter.acquire(-100) == 0


async def test_different_chats_do_not_block_each_other(limiter, clock):
    await limiter.acquire(-100)
    assert await limiter.acquire(-200) == 0


async def test_twenty_per_minute_ceiling_on_one_group(clock):
    """The Operations Group ceiling - the one that bites in practice."""
    limiter = RateLimiter(
        per_chat_interval=0, group_per_minute=20, clock=clock, sleep=clock.sleep
    )

    for _ in range(20):
        assert await limiter.acquire(-100) == 0
        clock.advance(0.1)

    waited = await limiter.acquire(-100)
    assert waited > 55, "21st message in a minute should be held back"


async def test_global_ceiling_applies_across_chats(clock):
    limiter = RateLimiter(
        per_chat_interval=0, group_per_minute=10_000,
        global_per_second=25, clock=clock, sleep=clock.sleep,
    )

    for chat_id in range(25):
        assert await limiter.acquire(-chat_id) == 0

    assert await limiter.acquire(-999) > 0


class Boom(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__("Too Many Requests")
        self.retry_after = retry_after


async def test_retries_after_a_429(clock):
    inner = FakeGateway()
    inner.fail_next = Boom(retry_after=3.0)
    gw = ThrottledGateway(
        inner,
        RateLimiter(clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
    )

    sent = await gw.send_message(-100, "hello")

    assert sent.message_id > 0, "should have retried and succeeded"
    assert 3.0 in clock.slept


async def test_non_429_errors_are_not_retried(clock):
    inner = FakeGateway()
    inner.fail_next = RuntimeError("chat not found")
    gw = ThrottledGateway(
        inner, RateLimiter(clock=clock, sleep=clock.sleep), sleep=clock.sleep
    )

    with pytest.raises(RuntimeError):
        await gw.send_message(-100, "hello")


async def test_throttle_passes_arguments_through(clock):
    inner = FakeGateway()
    gw = ThrottledGateway(
        inner, RateLimiter(clock=clock, sleep=clock.sleep), sleep=clock.sleep
    )

    await gw.send_message(-100, "hi", thread_id=77)
    await gw.send_file(-100, "fid", "document", thread_id=77)
    topic_id = await gw.create_topic(-100, "#1000 · Acme")
    await gw.close_topic(-100, topic_id)

    assert inner.calls[0].payload["thread_id"] == 77
    assert inner.calls[1].payload["file_id"] == "fid"
    assert inner.closed_topics == [(-100, topic_id)]
