"""Wiring checks.

These are cheap and they catch the class of mistake that only shows up when the
bot is live: a router registered in the wrong order, a callback that no handler
claims, a keyboard whose payload nobody can parse.
"""

from __future__ import annotations

import pytest

from app.bot import keyboards as kb
from app.bot.deps import work_item_for_thread
from app.domain.enums import Priority, WorkItemStatus


def test_callback_payloads_round_trip():
    for action in ["claim", "status", "priority", "close", "history", "back"]:
        assert kb.parse_cb(kb.cb(action, 42)) == (action, 42, None)

    for status in WorkItemStatus:
        data = kb.cb("setstatus", 1042, status.value)
        assert len(data.encode()) <= 64, "Telegram caps callback data at 64 bytes"
        assert kb.parse_cb(data) == ("setstatus", 1042, status.value)

    for priority in Priority:
        assert kb.parse_cb(kb.cb("setpriority", 7, priority.value))[2] == priority.value


def test_malformed_callback_is_rejected():
    with pytest.raises(ValueError):
        kb.parse_cb("garbage")
    with pytest.raises(ValueError):
        kb.parse_cb("other:claim:1")


def test_business_groups_get_the_commercial_wording():
    """PRD 15.4 - Business Operations is a commercial workflow, not a queue."""
    business = kb.raise_request_prompt("business")
    support = kb.raise_request_prompt("support")

    assert business.inline_keyboard[0][0].text == "Commercial Enquiry"
    assert support.inline_keyboard[0][0].text == "Raise Request"


def test_every_action_button_has_a_parseable_payload():
    markup = kb.work_item_actions(1042, claimed=False)
    for row in markup.inline_keyboard:
        for button in row:
            action, work_item_id, _ = kb.parse_cb(button.callback_data)
            assert work_item_id == 1042
            assert action


def test_claim_becomes_reassign_once_owned():
    unclaimed = kb.work_item_actions(1, claimed=False).inline_keyboard[0][0]
    claimed = kb.work_item_actions(1, claimed=True).inline_keyboard[0][0]

    assert unclaimed.text == "Claim"
    assert claimed.text == "Reassign"


def test_routers_are_ordered_so_the_catch_all_is_last():
    """The client router ends in a catch-all for group messages. If it were
    registered before the staff router, staff commands would fall into it."""
    from app.bot.main import build_dispatcher

    names = [r.name for r in build_dispatcher().sub_routers]
    assert names == ["admin", "staff", "client"]


async def test_topic_maps_back_to_its_work_item(session, acme_support, support_ops):
    from app.services.gateway import FakeGateway
    from app.services.relay import open_request

    gw = FakeGateway()
    item = await open_request(
        session, gw,
        source_chat=acme_support,
        subject="Settlement",
        body="body",
        raised_by_name="Tom Baker",
    )

    found = await work_item_for_thread(session, support_ops, item.topic_id)
    assert found is not None and found.id == item.id

    assert await work_item_for_thread(session, support_ops, 999999) is None
    assert await work_item_for_thread(session, support_ops, None) is None


def test_handler_modules_import_cleanly():
    """Catches import-time errors in handlers, which polling would only reveal
    on the first matching update."""
    from app.bot.handlers import admin, client, staff

    assert admin.router.name == "admin"
    assert client.router.name == "client"
    assert staff.router.name == "staff"
