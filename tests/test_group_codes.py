"""A short, permanent name for a counterparty's group.

Jason, 19 October: every client and supplier group gets a Group Code, set in
the group like a Lead, unique across the platform, changeable in place. The
first use is FX — `#BBS` picking the counterparty instead of a list — and the
one being built towards is broadcast targeting, so the code lives on the chat
and knows nothing about deals.

Two properties matter more than the rest, and both are the kind that fail
quietly:

  * every code that can be **set** must be one the parser can **find**;
  * a code must never reach the counterparty it names.
"""

from __future__ import annotations

import pytest

from app.domain import group_codes, shorthand
from app.domain.enums import Department

# --------------------------------------------------------------------------
# What a code may be
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("BBS", "BBS"),
    ("#BBS", "BBS"),
    ("bbs", "BBS"),
    ("  Acme ", "ACME"),
    ("WV", "WV"),
])
def test_codes_are_tidied_into_one_shape(raw, expected) -> None:
    assert group_codes.normalise(raw) == expected


@pytest.mark.parametrize("raw", ["A", "TOOLONG", "BB1", "", "   ", "B-S"])
def test_anything_the_parser_could_not_find_is_refused(raw) -> None:
    with pytest.raises(group_codes.BadGroupCode):
        group_codes.normalise(raw)


def test_every_settable_code_is_a_findable_code() -> None:
    """The coupling, asserted rather than trusted.

    A code that can be stored but never matched is the worst outcome here:
    the desk sets `#BBSFIN`, types it, nothing resolves, and nothing anywhere
    says why. Both patterns are built from `shorthand.CODE_BODY` so they
    cannot drift — this checks that they actually have not.
    """
    for candidate in ["BB", "BBS", "ACME", "wv", "Xy"]:
        code = group_codes.normalise(candidate)
        found = shorthand.find_codes(f"please ask #{code} about this")
        assert found == [code], (
            f"#{code} can be set but the parser does not find it in a sentence"
        )


def test_the_two_patterns_share_one_source() -> None:
    """Belt and braces: if somebody retypes one of them, this fails."""
    assert shorthand.CODE_BODY in shorthand.CODE_RE.pattern
    assert shorthand.CODE_BODY in group_codes.SETTABLE.pattern


# --------------------------------------------------------------------------
# Setting one
# --------------------------------------------------------------------------

async def test_a_group_gets_a_code(session, acme_support) -> None:
    previous = await group_codes.assign(session, acme_support, "ACME")

    assert previous is None
    assert acme_support.group_code == "ACME"
    assert await group_codes.holder_of(session, "ACME") is acme_support


async def test_changing_updates_in_place_and_says_what_it_was(
    session, acme_support
) -> None:
    """Jason: it updates the existing association rather than creating
    another one."""
    await group_codes.assign(session, acme_support, "ACME")
    previous = await group_codes.assign(session, acme_support, "ACM")

    assert previous == "ACME"
    assert acme_support.group_code == "ACM"
    # The old one stops meaning anything, rather than meaning this group too.
    assert await group_codes.holder_of(session, "ACME") is None


async def test_two_groups_cannot_share_a_code(
    session, acme_support, pexi_supplier
) -> None:
    """Uniqueness is global, not per desk.

    A code meaning two rooms is worse than no code: the person typing it
    cannot tell which one they got, and the first time they find out is when
    a client's figures are in a supplier's group.
    """
    await group_codes.assign(session, acme_support, "BBS")

    with pytest.raises(group_codes.CodeTaken):
        await group_codes.assign(session, pexi_supplier, "BBS")

    assert pexi_supplier.group_code is None


async def test_setting_the_same_code_on_the_same_group_is_fine(
    session, acme_support
) -> None:
    """Somebody re-running the command should not be told it is taken by
    themselves."""
    await group_codes.assign(session, acme_support, "ACME")
    assert await group_codes.assign(session, acme_support, "ACME") == "ACME"


# --------------------------------------------------------------------------
# Resolving one
# --------------------------------------------------------------------------

async def test_a_code_resolves_to_its_group(
    session, pexi_supplier, support_ops
) -> None:
    await group_codes.assign(session, pexi_supplier, "BBS")

    found = await group_codes.resolve(
        session, "#BBS", department=Department.SUPPORT, suppliers=True
    )
    assert found is pexi_supplier


async def test_an_unknown_code_stops_rather_than_guesses(session) -> None:
    """Jason: "If the code isn't recognised, or it can't confidently resolve
    the group, stop and tell Ops rather than guessing."
    """
    with pytest.raises(group_codes.GroupCodeError) as caught:
        await group_codes.resolve(
            session, "ZZZ", department=Department.SUPPORT, suppliers=True
        )
    assert "ZZZ" in str(caught.value)


async def test_the_wrong_side_of_the_trade_is_refused(
    session, acme_support
) -> None:
    """A client code handed to the supplier door.

    The expensive version of this mistake is a client's figures opening a
    conversation in a supplier's group, so it refuses by name rather than
    falling back to a list.
    """
    await group_codes.assign(session, acme_support, "ACME")

    with pytest.raises(group_codes.GroupCodeError) as caught:
        await group_codes.resolve(
            session, "ACME", department=Department.SUPPORT, suppliers=True
        )
    assert "client" in str(caught.value).lower()


async def test_another_desks_group_is_refused(
    session, acme_support, acme_compliance
) -> None:
    """Codes are global, permission is not."""
    await group_codes.assign(session, acme_compliance, "ACMC")

    with pytest.raises(group_codes.GroupCodeError):
        await group_codes.resolve(
            session, "ACMC", department=Department.SUPPORT, suppliers=False
        )


async def test_a_side_is_optional(session, acme_support) -> None:
    """Not every caller cares which side it is — broadcast targeting will
    not — so the narrowing is opt-in."""
    await group_codes.assign(session, acme_support, "ACME")

    found = await group_codes.resolve(
        session, "ACME", department=Department.SUPPORT
    )
    assert found is acme_support


# --------------------------------------------------------------------------
# It never leaves the building
# --------------------------------------------------------------------------

def test_any_code_that_can_be_set_can_be_stripped() -> None:
    """Jason: "#BBS remains completely internal and must never go out to the
    client/supplier."

    Checked against the settable range rather than one example, because the
    leak is silent: a supplier reading `#ACME` has learned the client's code,
    which is the beginning of working out NexterPay's margin.
    """
    for candidate in ["BB", "BBS", "ACME"]:
        code = group_codes.normalise(candidate)
        body = f"can you do 611 for #{code} today"
        assert f"#{code}" not in shorthand.strip_codes(body)
        assert "611" in shorthand.strip_codes(body)


def test_stripping_leaves_a_sentence_somebody_would_send() -> None:
    assert shorthand.strip_codes("#BBS what is your rate today?") == (
        "what is your rate today?"
    )


# --------------------------------------------------------------------------
# Where FX uses it
# --------------------------------------------------------------------------
#
# `/npnewsu #BBS` opens the request with that supplier instead of showing a
# list. Driven through the real handler rather than asserted about the source,
# because the thing worth checking is which group ends up in the state the
# next step reads.

class _Said:
    """A stand-in for the Operations Group, remembering what it was told."""

    def __init__(self, text: str, chat_id: int, user) -> None:
        self.text = text
        self.chat = type("c", (), {"id": chat_id, "type": "supergroup"})()
        self.from_user = user
        self.caption = None
        self.said: list[str] = []

    async def reply(self, text, **_kw):
        self.said.append(text)
        return None

    async def answer(self, text, **_kw):
        self.said.append(text)
        return None


class _State:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def clear(self) -> None:
        self.data, self.state = {}, None

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def get_data(self) -> dict:
        return dict(self.data)


async def _run_newsu(session, monkeypatch, ops, operator, typed, *, suppliers=True):
    import contextlib

    from app.bot.handlers import outbound

    @contextlib.asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(outbound, "session_scope", fake_scope)

    user = type("u", (), {"id": operator.telegram_user_id, "full_name": "Sarah Hill"})()
    message = _Said(typed, ops.telegram_chat_id, user)
    state = _State()
    await outbound._start(
        message, state,
        suppliers=suppliers, noun="supplier" if suppliers else "client",
    )
    return message, state


async def test_a_code_opens_the_request_without_the_list(
    session, support_ops, pexi_supplier, operator, monkeypatch
) -> None:
    await group_codes.assign(session, pexi_supplier, "BBS")

    message, state = await _run_newsu(
        session, monkeypatch, support_ops, operator, "/npnewsu #BBS"
    )

    assert state.data.get("to_chat_id") == pexi_supplier.telegram_chat_id, (
        "the code did not pick the group — the desk would still be choosing "
        "from a list"
    )
    assert "Which supplier" not in " ".join(message.said)


async def test_no_code_still_shows_the_list(
    session, support_ops, pexi_supplier, operator, monkeypatch
) -> None:
    """Jason: if there is no Group Code, the existing FX journey just
    continues as it does now."""
    await group_codes.assign(session, pexi_supplier, "BBS")

    message, state = await _run_newsu(
        session, monkeypatch, support_ops, operator, "/npnewsu"
    )

    assert state.data.get("to_chat_id") is None
    assert any("Which supplier" in said for said in message.said)


async def test_an_unknown_code_refuses_instead_of_falling_back(
    session, support_ops, pexi_supplier, operator, monkeypatch
) -> None:
    """Falling through to the picker would be friendlier and wrong.

    The desk typed a code because they believed it meant something. Quietly
    showing a list teaches them it worked, and the next time they will not be
    watching.
    """
    message, state = await _run_newsu(
        session, monkeypatch, support_ops, operator, "/npnewsu #ZZZ"
    )

    assert state.data.get("to_chat_id") is None
    joined = " ".join(message.said)
    assert "ZZZ" in joined
    assert "Which supplier" not in joined


async def test_a_client_code_at_the_supplier_door_refuses(
    session, support_ops, acme_support, operator, monkeypatch
) -> None:
    await group_codes.assign(session, acme_support, "ACME")

    message, state = await _run_newsu(
        session, monkeypatch, support_ops, operator, "/npnewsu #ACME"
    )

    assert state.data.get("to_chat_id") is None
    assert "client" in " ".join(message.said).lower()


async def test_two_codes_will_not_be_chosen_between(
    session, support_ops, pexi_supplier, acme_support, operator, monkeypatch
) -> None:
    await group_codes.assign(session, pexi_supplier, "BBS")
    await group_codes.assign(session, acme_support, "ACME")

    message, state = await _run_newsu(
        session, monkeypatch, support_ops, operator, "/npnewsu #BBS and #ACME"
    )

    assert state.data.get("to_chat_id") is None
    assert "#BBS" in " ".join(message.said)


def test_every_refusal_on_the_platform_is_a_domain_error() -> None:
    """`deps.explain` shows a DomainError to staff as written, and turns
    everything else into "Something went wrong. The error has been logged."

    So an exception class meant to be read by a person, that is not a
    DomainError, is a carefully worded message nobody will ever see. Found by
    fuzzing on 25 October: `WalletError` was the only one, and `/npwallet`
    only escaped it by catching the class by name.

    Enumerated rather than named, so the next one fails here instead.
    """
    import importlib
    import inspect
    import pkgutil

    from app.domain.errors import DomainError

    offenders = []
    for package in ("app.domain", "app.services"):
        module = importlib.import_module(package)
        for info in pkgutil.iter_modules(module.__path__):
            name = f"{package}.{info.name}"
            try:
                loaded = importlib.import_module(name)
            except Exception:          # a module needing a live service
                continue
            for attr, value in vars(loaded).items():
                if not inspect.isclass(value) or not issubclass(value, Exception):
                    continue
                if value.__module__ != name:
                    continue
                if not attr.endswith("Error"):
                    continue
                if not issubclass(value, DomainError):
                    offenders.append(f"{name}.{attr}")

    assert not offenders, (
        "these are raised at people but are not DomainErrors, so explain() "
        f"replaces them with a generic apology: {offenders}"
    )


# --------------------------------------------------------------------------
# The code must not escape in anything a counterparty reads
# --------------------------------------------------------------------------
#
# Jason, 19 October: "#BBS remains completely internal and must never go out
# to the client/supplier."
#
# Found on 25 October hunting leaks. `outbound_body` had stripped codes from
# the body since the shorthand existed - but a request's subject is the first
# line the desk typed, rendered separately, and nothing stripped that. Since
# group codes arrived, a first line beginning `#BBS` is the normal way to
# write one.

@pytest.mark.parametrize("typed,expected", [
    ("#BBS can you quote XOF for today", "can you quote XOF for today"),
    ("Can you quote XOF\n#BBS please", "Can you quote XOF"),
    ("#BBS", "New request"),
    ("Nothing internal here", "Nothing internal here"),
])
def test_a_subject_loses_the_shorthand_on_the_way_out(typed, expected) -> None:
    from types import SimpleNamespace

    from app.services import relay

    subject = (typed.splitlines()[0] if typed else "")[:120] or "New request"
    item = SimpleNamespace(client_reference="ACME-1042", subject=subject)

    assert relay.outward_subject(item) == expected

    opening = relay.outbound_opening_text(item, typed)
    assert "#BBS" not in opening, (
        "the counterparty was sent the desk's own shorthand in the subject"
    )


async def test_the_topic_keeps_the_shorthand(session, acme_support) -> None:
    """Internally it is useful: the Operations topic is titled from the
    subject, and `#BBS` there says which supplier the request is with.

    Only the outward rendering strips, which is why there are two functions
    and not one.
    """
    from app.domain import work_items as wi
    from app.services import relay

    item = await wi.create_work_item(
        session, source_chat=acme_support, subject="#BBS can you quote XOF",
        original_message="#BBS can you quote XOF", raised_by_name="Sarah",
    )

    assert "#BBS" in relay.topic_name(item, "Acme Ltd")
    assert relay.outward_subject(item) == "can you quote XOF"


def test_every_outward_rendering_of_a_subject_goes_through_one_function() -> None:
    """The class, so the next outward path cannot forget.

    `item.subject` read directly inside a function that sends to a
    counterparty is the shape of this fault. Internal renderings - the topic
    name, the Operations header - are meant to keep the code and are named
    here rather than discovered.
    """
    import ast
    import pathlib

    internal = {"topic_name", "header_text"}

    source = pathlib.Path("app/services/relay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in internal or node.name == "outward_subject":
            continue
        body = ast.unparse(node)
        if "item.subject" not in body:
            continue
        # Reading it to pass to outward_subject or outbound_body is fine.
        bare = body.replace("outward_subject(item)", "").replace(
            "outbound_body(item.subject or '', body)", ""
        )
        if "item.subject" in bare and "send_message" in body:
            offenders.append(node.name)

    assert not offenders, (
        f"these send a raw subject to a counterparty: {offenders}. Use "
        f"relay.outward_subject(item)."
    )
