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

# Two to four letters after a hash, not glued to a word on either side.
#
# Two is the floor because country codes are two letters and the desk already
# writes them. Four is the ceiling because that is the width of a counterparty
# code. The trailing boundary stops `#BBSX` matching as BBS, which matters:
# half a code resolving to the wrong counterparty is worse than not resolving.
CODE_RE = re.compile(r"(?<![\w#])#([A-Za-z]{2,4})(?![\w])")


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
    """
    cleaned = CODE_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


__all__ = ["CODE_RE", "find_codes", "strip_codes", "unique_codes"]
