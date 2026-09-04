"""Every gateway implementation must implement the whole Protocol.

There are three: AiogramGateway talks to Telegram, FakeGateway is what the
tests use, and ThrottledGateway wraps the real one with rate limiting and is
what actually runs in production.

This file exists because of a specific failure. `rename_topic` was added to
the Protocol, to AiogramGateway and to FakeGateway, but not to
ThrottledGateway. Every test passed - they all use the fake directly - and
filing a request under a supplier crashed the first time a human tried it,
with AttributeError on the object that only exists in production.

A Protocol is not checked at runtime, so nothing catches this by itself.
"""

from __future__ import annotations

import inspect

import pytest

from app.services.gateway import AiogramGateway, FakeGateway, TelegramGateway
from app.services.throttle import ThrottledGateway

IMPLEMENTATIONS = [AiogramGateway, FakeGateway, ThrottledGateway]


def _protocol_methods() -> set[str]:
    return {
        name for name, member in inspect.getmembers(TelegramGateway)
        if not name.startswith("_") and inspect.isfunction(member)
    }


@pytest.mark.parametrize("implementation", IMPLEMENTATIONS, ids=lambda c: c.__name__)
def test_implements_every_gateway_method(implementation) -> None:
    missing = sorted(_protocol_methods() - set(dir(implementation)))
    assert not missing, (
        f"{implementation.__name__} is missing {missing}. "
        f"Adding a method to the gateway means adding it to all three - the "
        f"Protocol is not enforced at runtime, so the gap only appears when "
        f"someone uses the feature."
    )


def test_the_throttled_wrapper_forwards_rather_than_reimplements() -> None:
    """It should delegate, not duplicate.

    ThrottledGateway exists to add rate limiting and retries. If a method on
    it does anything other than pass through to the inner gateway, the two
    can drift apart in behaviour without any test noticing.
    """
    source = inspect.getsource(ThrottledGateway)
    for name in _protocol_methods():
        if f"def {name}(" in source:
            body = source.split(f"def {name}(", 1)[1].split("async def ")[0]
            assert f"self._inner.{name}" in body, (
                f"ThrottledGateway.{name} does not forward to the inner gateway"
            )


@pytest.mark.parametrize("implementation", IMPLEMENTATIONS, ids=lambda c: c.__name__)
def test_the_signatures_match_and_not_only_the_names(implementation) -> None:
    """Presence is not the whole contract.

    The test above catches a missing method. It does not catch one that takes
    different arguments - and that is the more likely mistake, because adding
    a parameter to the Protocol and to two of the three implementations is
    exactly what happens when you are halfway through a change.

    Found this way: `parse_mode` was added to `edit_message_text` so the topic
    header could be bold, and ThrottledGateway's `reply_markup` had drifted to
    having no annotation at all. Harmless in that instance, and the next one
    would not be.

    ThrottledGateway forwards with **kwargs by design, so a signature carrying
    those is accepted.
    """
    for name in sorted(_protocol_methods()):
        expected = inspect.signature(getattr(TelegramGateway, name))
        actual = inspect.signature(getattr(implementation, name))
        if "**kwargs" in str(actual):
            continue
        assert str(actual) == str(expected), (
            f"{implementation.__name__}.{name} does not match the Protocol.\n"
            f"  protocol: {expected}\n"
            f"  actual:   {actual}"
        )
