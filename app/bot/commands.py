"""Command names, defined in one place.

Every command carries an `np` prefix. This is not decoration - it solves two
distinct problems that appear once a second bot shares a group with this one.

Telegram delivers a bare command such as `/raise` to a bot only if that bot was
the last one to post, so with another bot present the command becomes
intermittent: it works when it is tested and fails a week later, with no error
anywhere to explain it. And if both bots happen to implement the same name,
both answer. Admin rights fix the first problem but not the second. A unique
prefix removes both, permanently, without either team having to check the
other's command list again.

There is no underscore after the prefix, and none inside a name either.
NexterPay asked for them out on 3 September, having lived with `np_raise` for
a week. `/npraise` it is.

`/start` is the one exception and is deliberately left unprefixed: Telegram's
own interface sends it when someone taps Start on the bot, so it has to keep
working under that name. `/npstart` is registered alongside it.

Names are bare here, without the leading slash, because that is the form
aiogram's Command filter expects. Use `slash()` when showing one to a person.
"""

from __future__ import annotations

from aiogram.filters import Command

PREFIX = "np"


def _c(name: str) -> str:
    return f"{PREFIX}{name}"


def slash(name: str) -> str:
    """The form a person types, for use in messages and help text."""
    return f"/{name}"


def any_case(*names: str) -> Command:
    """A command filter that accepts whatever capitalisation someone types.

    People write /NPRAISE. Telegram does not normalise the case of a command,
    and aiogram's filter is case-sensitive by default, so without this the
    message is simply not delivered to the handler and the bot appears to
    ignore them. A command that silently does nothing is the worst failure
    this platform can produce - there is no error to report and nothing in the
    log that a person would think to look for.
    """
    return Command(*names, ignore_case=True)


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
LINK = _c("link")
UNLINK = _c("unlink")

# Administration. The three register commands are the longest names on the
# platform, and deliberately so - they are run once per group by one person,
# and being unmistakable matters more than being short.
REGISTER_OPS = _c("registerops")
REGISTER_CLIENT = _c("registerclient")
REGISTER_SUPPLIER = _c("registersupplier")
ADDUSER = _c("adduser")
REMOVEUSER = _c("removeuser")
WORKLOAD = _c("workload")
SETCODE = _c("setcode")
ADDPARTY = _c("addparty")
BROADCAST = _c("broadcast")

# Naming the people inside a counterparty group. Telegram will not list them,
# so they are registered the same way staff are - by replying to one of their
# messages.
SETLEAD = _c("setlead")
LEADS = _c("leads")
REMOVELEAD = _c("removelead")

# The front door for administration. Two jobs are done under time pressure
# with somebody waiting - registering a group and adding a person - and both
# are easier to get wrong from memory than from a list.
SETUP = _c("setup")

# What can I do, from here. Department-aware and role-aware, because the
# honest answer to that question depends on both - and because a guide nobody
# has open is worth less than a command anybody can send.
HELP = _c("help")

# The whole permission ladder, for reference. NexterPay asked for it in the
# group rather than in a document, which is right: a document about who can do
# what is out of date the first time a threshold moves, and nobody re-reads it
# anyway. This one is generated from the checks themselves.
ROLE = _c("role")

# Raising outbound, split by who it goes to. One command with a picker was
# workable, but the picker is where you discovered which kind of counterparty
# you were about to open a conversation with. Now the intent is in the command.
NEW_CLIENT = _c("newcl")
NEW_SUPPLIER = _c("newsu")

# Anywhere
START = "start"
START_ALIAS = _c("start")
WHOAMI = _c("whoami")

# Every name this bot answers to. Used by the tests to prove nothing has been
# left unprefixed, and nothing has kept an underscore, by accident.
ALL = [
    FRONT_DOOR,
    RAISE, REQUEST, ENQUIRY, TICKETS,
    REPLY, NOTE, HISTORY, ASSIGN, LINK, UNLINK,
    REGISTER_OPS, REGISTER_CLIENT, REGISTER_SUPPLIER, ADDUSER, REMOVEUSER,
    WORKLOAD, SETCODE, ADDPARTY, BROADCAST,
    SETLEAD, LEADS, REMOVELEAD, SETUP, HELP, ROLE,
    NEW_CLIENT, NEW_SUPPLIER,
    START, START_ALIAS, WHOAMI,
]
