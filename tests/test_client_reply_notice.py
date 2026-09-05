"""The message that tells an owner their client has come back.

NexterPay asked for this one "in a different colour". Telegram gives a bot no
colour at all - the whole set of styles available is bold, italic, underline,
strikethrough, spoiler, code, pre, blockquote, links and mentions, and not one
of them changes the colour of text. A blockquote is the strongest thing on
that list, and it is also the honest markup: this is somebody else's words
quoted into our group.
"""

from __future__ import annotations

import pytest

from app.domain.work_items import Actor
from app.services import relay
from app.services.gateway import FakeGateway

OPS_CHAT = -1001000000001


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()


async def _raised(session, gw, chat):
    return await relay.open_request(
        session, gw, source_chat=chat, subject="Settlement missing",
        body="No settlement for 3 March.", raised_by_name="Tom Baker",
        raised_by_telegram_user_id=9001,
    )


def _notice_call(gw: FakeGateway):
    """The "has replied" notification, not the history line that follows it.

    Picked by content rather than by position. `announce()` posts a history
    entry immediately afterwards, so "the last message" is the wrong message -
    and a test that asserted against the history line would pass while the
    notification itself was empty.
    """
    matches = [
        c for c in gw.calls
        if c.chat_id == OPS_CHAT and c.method == "send_message"
        and "has replied" in c.payload.get("text", "")
    ]
    assert matches, "no client-reply notification was sent at all"
    return matches[-1]


def _notice(gw: FakeGateway) -> str:
    return _notice_call(gw).payload["text"]


async def test_the_clients_words_arrive_quoted(
    session, acme_support, support_ops, operator, gw
):
    item = await _raised(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="No please check again",
        sender_name="Gavs D",
        telegram_message_id=555,
        sender_telegram_user_id=4242,
    )
    notice = _notice(gw)

    assert "<blockquote>No please check again</blockquote>" in notice
    assert "<b>Gavs D has replied</b>" in notice
    assert item.display_reference in notice


async def test_it_still_mentions_the_owner(
    session, acme_support, support_ops, operator, gw
):
    """The block is decoration. The mention is the part that actually reaches
    a person, and styling it must not have cost that."""
    item = await _raised(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item, text="any news?", sender_name="Gavs D",
        telegram_message_id=556,
    )
    assert f'tg://user?id={operator.telegram_user_id}' in _notice(gw)


async def test_an_unclaimed_request_is_quoted_too(
    session, acme_support, support_ops, gw
):
    """The unowned branch used to be plain text with no styling at all, so
    the same message looked different depending on whether anyone had picked
    the request up - which is the one thing it should not depend on."""
    item = await _raised(session, gw, acme_support)

    await relay.relay_client_message(
        session, gw, item, text="still waiting", sender_name="Gavs D",
        telegram_message_id=557,
    )
    notice = _notice(gw)

    assert "<blockquote>still waiting</blockquote>" in notice
    assert "<b>Gavs D has replied</b>" in notice


async def test_the_notice_is_sent_as_html(
    session, acme_support, support_ops, gw
):
    """Without parse_mode the tags are delivered as literal text.

    This exact mistake has already been made once, on the owner mention: the
    person saw raw HTML and was never pinged. Here it would put
    "<blockquote>" in front of the client's words in the Operations Group.
    """
    item = await _raised(session, gw, acme_support)
    await relay.relay_client_message(
        session, gw, item, text="hello", sender_name="Gavs D",
        telegram_message_id=558,
    )
    assert _notice_call(gw).payload["parse_mode"] == "HTML"


async def test_a_client_cannot_inject_markup(
    session, acme_support, support_ops, gw
):
    """Their message is escaped before it is wrapped.

    A client typing a "<" would otherwise either break the markup or, worse,
    have it interpreted - and the text in here is the one part of the message
    that comes from outside NexterPay entirely.
    """
    item = await _raised(session, gw, acme_support)
    await relay.relay_client_message(
        session, gw, item,
        text="is <b>this</b> </blockquote> escaped?",
        sender_name="Gavs <script> D",
        telegram_message_id=559,
    )
    notice = _notice(gw)

    assert "&lt;b&gt;this&lt;/b&gt;" in notice
    assert "&lt;/blockquote&gt;" in notice
    assert "Gavs &lt;script&gt; D" in notice
    # Exactly one real block: theirs, opened and closed by us.
    assert notice.count("<blockquote>") == 1
    assert notice.count("</blockquote>") == 1


async def test_nothing_of_this_reaches_the_client(
    session, acme_support, support_ops, operator, gw
):
    """It is a notification to staff about a message the client just sent.
    Echoing any of it back into their group would be absurd, and is the kind
    of absurd that only shows up in front of a customer."""
    item = await _raised(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))
    before = len(gw.messages_to(acme_support.telegram_chat_id))

    await relay.relay_client_message(
        session, gw, item, text="anything?", sender_name="Gavs D",
        telegram_message_id=560,
    )

    after = gw.messages_to(acme_support.telegram_chat_id)
    assert len(after) == before, f"the client was sent something: {after[before:]}"


# --------------------------------------------------------------------------
# What they were replying to
#
# NexterPay, 5 September. Telegram shows the client the message they are
# quoting; we passed on only what they typed. So a client answering one
# specific reply among several - "no, the other one" - arrived as "no, the
# other one" and nothing else, and whoever picked it up had to guess.
# --------------------------------------------------------------------------


async def test_the_quoted_message_travels_with_the_reply(
    session, acme_support, support_ops, operator, gw
):
    item = await _raised(session, gw, acme_support)
    await relay.claim(session, gw, item, Actor.of(operator))

    await relay.relay_client_message(
        session, gw, item,
        text="no, the other one",
        sender_name="Gavs D",
        telegram_message_id=901,
        replying_to="We have raised this with the card scheme and will update you.",
    )
    notice = _notice(gw)

    assert "in reply to" in notice
    assert "raised this with the card scheme" in notice
    assert "<blockquote>no, the other one</blockquote>" in notice


async def test_it_is_absent_when_they_replied_to_nothing(
    session, acme_support, support_ops, gw
):
    """A line saying "in reply to: " with nothing after it is worse than no
    line, and most replies arrive without one."""
    item = await _raised(session, gw, acme_support)
    await relay.relay_client_message(
        session, gw, item, text="any news?", sender_name="Gavs D",
        telegram_message_id=902,
    )
    assert "in reply to" not in _notice(gw)


async def test_a_long_quote_is_trimmed(
    session, acme_support, support_ops, gw
):
    """Context, not content. The message it belongs to is a few lines up the
    topic anyway, and this must not bury what they actually said."""
    item = await _raised(session, gw, acme_support)
    await relay.relay_client_message(
        session, gw, item, text="yes", sender_name="Gavs D",
        telegram_message_id=903, replying_to="word " * 200,
    )
    notice = _notice(gw)

    assert "…" in notice
    assert len(notice) < 700, f"{len(notice)} characters for a one-word reply"
    assert "<blockquote>yes</blockquote>" in notice


async def test_the_quote_cannot_break_the_markup(
    session, acme_support, support_ops, gw
):
    """It is text we received from outside NexterPay, exactly like the reply
    itself, and it goes into a message with markup in it."""
    item = await _raised(session, gw, acme_support)
    await relay.relay_client_message(
        session, gw, item, text="ok", sender_name="Gavs D",
        telegram_message_id=904, replying_to="see <b>this</b> </blockquote>",
    )
    notice = _notice(gw)

    assert "&lt;b&gt;this&lt;/b&gt;" in notice
    assert notice.count("<blockquote>") == 1


def test_the_client_prompt_is_emphasised() -> None:
    """NexterPay asked for it bold: it is the only place a client is told they
    can attach a screenshot, and it was reading as ordinary chat."""
    from app.bot.deps import prompt_for

    class _User:
        full_name = "Gavs D"
        id = 4242

    plain, _, _ = prompt_for(_User(), "Please describe the issue.")
    bold, _, _ = prompt_for(_User(), "Please describe the issue.", emphasise=True)

    assert "<b>" not in plain, "the staff prompts should not be shouting"
    assert "<b>please describe the issue.</b>" in bold
