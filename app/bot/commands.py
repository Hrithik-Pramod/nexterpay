"""Command names, defined in one place.

Every command carries an `np_` prefix. This is not decoration - it solves two
distinct problems that appear once a second bot shares a group with this one.

Telegram delivers a bare command such as `/raise` to a bot only if that bot was
the last one to post, so with another bot present the command becomes
intermittent. And if both bots happen to implement the same name, both answer.
Admin rights fix the first problem but not the second. A unique prefix removes
both, permanently, without either team having to check the other's command
list again.

`/start` is the one exception and is deliberately left unprefixed: Telegram's
own interface sends it when someone taps Start on the bot, so it has to keep
working under that name. `/np_start` is registered alongside it.

Names are bare here, without the leading slash, because that is the form
aiogram's Command filter expects. Use `slash()` when showing one to a person.
"""

from __future__ import annotations

PREFIX = "np"


def _c(name: str) -> str:
    return f"{PREFIX}_{name}"


def slash(name: str) -> str:
    """The form a person types, for use in messages and help text."""
    return f"/{name}"


# The front door. `/np` on its own brings up the menu, which is the one thing
# clients are asked to remember.
FRONT_DOOR = PREFIX

# Client-facing
RAISE = _c("raise")
REQUEST = _c("request")
ENQUIRY = _c("enquiry")
TICKETS = _c("tickets")

# Staff, inside an Operations Group
REPLY = _c("reply")
NOTE = _c("note")
HISTORY = _c("history")
ASSIGN = _c("assign")

# Administration
REGISTER_OPS = _c("register_ops")
REGISTER_CLIENT = _c("register_client")
ADDUSER = _c("adduser")
REMOVEUSER = _c("removeuser")
WORKLOAD = _c("workload")
SETCODE = _c("setcode")
ADDPARTY = _c("addparty")

# Anywhere
START = "start"
START_ALIAS = _c("start")
WHOAMI = _c("whoami")

# Every name this bot answers to. Used by the tests to prove nothing has been
# left unprefixed by accident.
ALL = [
    FRONT_DOOR,
    RAISE, REQUEST, ENQUIRY, TICKETS,
    REPLY, NOTE, HISTORY, ASSIGN,
    REGISTER_OPS, REGISTER_CLIENT, ADDUSER, REMOVEUSER, WORKLOAD, SETCODE, ADDPARTY,
    START, START_ALIAS, WHOAMI,
]
