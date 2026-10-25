"""Naming a counterparty in the middle of a sentence.

Jason, 3 October: "ideally when he is messaging a group he can use @BBS or
@ACME". The desk works in prose, and the platform needs to know which
counterparty a sentence is about without anybody stopping to pick one from a
list.

**It is `#BBS` rather than `@BBS`, and that was a real decision.** Telegram
clients auto-link anything that looks like `@word` into a username, so `@BBS`
renders as a tappable link to an account that does not exist - every time,
in every group, for ever. Raised with Jason as a detail worth settling before
anybody got used to it; he picked `#`.

The codes themselves already exist. Every counterparty carries a four-letter
code - it is what leads every reference and every topic title - so this is a
way of writing one down mid-sentence rather than a new idea.

Nothing here resolves anything on its own. Parsing is separated from lookup
so that the parsing can be tested against the awkward cases - a code at the
end of a sentence, inside brackets, next to punctuation - without a database,
and so the lookup has one place to be careful about who is allowed to see
which counterparty.
"""

from __future__ import annotations

import re

# What a code is made of, on its own, so that the one rule has one home.
#
# Two is the floor because country codes are two letters and the desk already
# writes them. Four is the ceiling because that is the width of a counterparty
# code.
#
# Separated out because `group_codes.SETTABLE` has to accept exactly what this
# can find. A code that can be set but never matched is worse than one that is
# refused: the desk types `#BBSFIN`, nothing resolves, and nothing anywhere
# explains why. Both patterns are now built from this fragment, so widening one
# widens the other on the same day.
CODE_BODY = r"[A-Za-z]{2,4}"

# The same thing in a sentence: after a hash, not glued to a word on either
# side. The trailing boundary stops `#BBSX` matching as BBS, which matters -
# half a code resolving to the wrong counterparty is worse than not resolving.
CODE_RE = re.compile(rf"(?<![\w#])#({CODE_BODY})(?![\w])")


def find_codes(text: str) -> list[str]:
    """Every `#CODE` in a message, upper-cased, in the order written.

    Duplicates are kept rather than collapsed. "#BBS quoted 611, #BBS can do
    612" is one counterparty twice, and the caller may care which mention it
    is acting on.
    """
    return [match.group(1).upper() for match in CODE_RE.finditer(text or "")]


def unique_codes(text: str) -> list[str]:
    """The distinct counterparties a message names, first mention first."""
    seen: list[str] = []
    for code in find_codes(text):
        if code not in seen:
            seen.append(code)
    return seen


def strip_codes(text: str) -> str:
    """The message without the shorthand, for when it is being relayed on.

    A counterparty does not need to see the platform's internal labels, and a
    supplier reading `#ACME` has just learned the client's code - which is the
    beginning of working out what NexterPay make on them.

    Takes the punctuation with it. Removing a word from the middle of a
    sentence leaves the spacing around it behind, and the result went out to a
    supplier on 25 October reading:

        Noting this is for , same terms as last week.

    The code had gone, which is the part that matters, and what arrived was a
    sentence with a hole in it. A counterparty cannot tell a redaction from a
    broken bot, and the one they will assume is the one that makes them doubt
    the figures in the same message.

    This cannot rescue the grammar - "for , same" becomes "for, same", not
    whatever the desk meant - so it buys a typo in place of something that
    reads like a fault. The real answer is upstream: the desk sees the
    outgoing text in the preview before anything is sent, and that preview
    now shows exactly this.
    """
    kept: list[str] = []
    for line in (text or "").splitlines():
        tidied = _tidy(CODE_RE.sub("", line))
        if tidied or not (line.strip() and not tidied):
            # Blank lines the desk typed are paragraph breaks and stay. A line
            # that held something and now holds nothing held only codes, and
            # leaving it behind turns "#BBS" on its own line into a gap.
            kept.append(tidied)
    return "\n".join(kept).strip()


def _tidy(line: str) -> str:
    """One line, with the spacing the removal left behind cleaned up."""
    # A space stranded before punctuation by the removal.
    line = re.sub(r"[ \t]+([,.;:!?)\]])", r"\1", line)
    # An opening bracket left hanging against whatever followed the code.
    line = re.sub(r"([(\[])[ \t]+", r"\1", line)
    # Brackets emptied out entirely - "(#BBS)" should not become "()".
    line = re.sub(r"\(\s*\)|\[\s*\]", "", line)
    return re.sub(r"[ \t]{2,}", " ", line).strip()


__all__ = ["CODE_BODY", "CODE_RE", "find_codes", "strip_codes", "unique_codes"]
