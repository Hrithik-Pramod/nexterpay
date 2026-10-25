"""Telegram refuses a message over 4096 characters.

Found on 25 October, measuring the unbounded outputs ahead of the November
launch. Two of them pass the limit on numbers a real desk could reach:

  * `/npbook` at about seventy open deals;
  * the settlement preview at about sixty lines.

Neither send was wrapped in anything, and Telegram's refusal is an exception
rather than a truncation - so the desk typed `/npbook`, aiogram logged a
`message is too long`, and nothing at all came back. A command that does
nothing and says nothing.

It matters more than an ordinary silence. The quiet rules of 7 October rest
on the book: the platform stopped narrating on the grounds that the desk can
ask whenever they like. The busier the desk, the longer the book - so the one
thing holding the silence up is the thing that breaks first, exactly when it
is needed.

Split rather than truncated. Both of these are lists whose whole purpose is to
be complete; cutting the end off hides the rows nobody then knows to look for.
"""

from __future__ import annotations

from app.bot.deps import CHUNK, TELEGRAM_LIMIT, in_parts, say


def test_a_short_message_is_left_alone() -> None:
    assert in_parts("Outstanding book — 3 open") == ["Outstanding book — 3 open"]


def test_a_long_message_is_split_under_the_limit() -> None:
    text = "\n".join(
        f"<b>FXACME-SPEX-{1000 + i}</b> · Awaiting settlement · 2d · ACME"
        for i in range(200)
    )
    parts = in_parts(text)

    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert all(len(part) <= CHUNK for part in parts)


def test_nothing_is_lost_in_the_split() -> None:
    """The reason it is a split and not a truncation."""
    text = "\n".join(f"line {i} of the outstanding book" for i in range(400))
    rejoined = "".join(in_parts(text))

    assert rejoined.replace("\n", "") == text.replace("\n", "")
    for i in (0, 123, 399):
        assert f"line {i} of the outstanding book" in rejoined


def test_it_breaks_on_lines_so_html_survives() -> None:
    """A cut through the middle of a tag is a message Telegram rejects
    outright - one invisible failure traded for another."""
    text = "\n".join(f"<b>row {i}</b> — something about a deal" for i in range(300))

    for part in in_parts(text):
        assert part.count("<b>") == part.count("</b>"), (
            "a part was cut through an HTML tag"
        )


def test_a_single_enormous_line_is_still_sendable() -> None:
    """Nothing on this platform writes one, which is exactly why it would be
    the thing that broke."""
    parts = in_parts("x" * 9000)
    assert all(len(part) <= CHUNK for part in parts)
    assert "".join(parts) == "x" * 9000


async def test_the_keyboard_goes_on_the_last_part_only() -> None:
    """A button belongs under the end of the thing it acts on, and four
    copies of Record this settlement is four chances to record it twice."""
    sent: list[tuple[str, object]] = []

    async def fake_send(text, **kw):
        sent.append((text, kw.get("reply_markup")))

    await say(
        fake_send,
        "\n".join(f"line {i}" for i in range(2000)),
        parse_mode="HTML",
        reply_markup={"record": True},
    )

    assert len(sent) > 1
    assert [markup for _, markup in sent[:-1]] == [None] * (len(sent) - 1)
    assert sent[-1][1] == {"record": True}


async def test_a_short_message_still_carries_its_keyboard() -> None:
    sent: list[tuple[str, object]] = []

    async def fake_send(text, **kw):
        sent.append((text, kw.get("reply_markup")))

    await say(fake_send, "Settlement — nothing saved yet", reply_markup={"k": 1})

    assert len(sent) == 1
    assert sent[0][1] == {"k": 1}
