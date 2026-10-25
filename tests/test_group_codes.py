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


async def test_the_refusal_does_not_name_the_group_holding_the_code(
    session, acme_support, pexi_supplier
) -> None:
    """A client must not learn a supplier exists from a failed admin command.

    `/npsetgroupcode` is typed inside the counterparty's own room - that is
    the design, because the group you are standing in is the group being
    named - so this refusal is read by that counterparty.

    Found live on 25 October, running `/npsetgroupcode BBS` in a client
    group, which answered:

        BBS already belongs to TEST - Pexi Finance. Codes have to be unique
        or #BBS means two different rooms.

    Acme had just been told the name of a supplier's group. The refusal still
    has to be useful to the administrator, so it points at `/npgroups`, which
    is read in an Operations Group where naming rooms costs nothing.
    """
    await group_codes.assign(session, acme_support, "BBS")

    with pytest.raises(group_codes.CodeTaken) as caught:
        await group_codes.assign(session, pexi_supplier, "BBS")

    said = str(caught.value)
    assert acme_support.title
    assert acme_support.title not in said, (
        f"the holder's group title reached a counterparty's room: {said!r}"
    )
    for word in acme_support.title.replace("—", " ").replace("-", " ").split():
        assert word.lower() not in said.lower(), (
            f"{word!r} from the holder's title reached a counterparty's room"
        )

    # Still useful: it says what to do, and warns that the list it points at
    # is per desk while codes are unique platform-wide.
    assert "/npgroups" in said
    assert "another desk" in said

    # And nothing is lost internally - a caller standing somewhere safe can
    # still say which group it was.
    assert caught.value.holder is acme_support


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


# --------------------------------------------------------------------------
# What a counterparty reads when a code is removed from the middle of it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("typed,expected", [
    # The one that went out live, 25 October, to a supplier.
    (
        "Noting this is for #BBS, same terms as last week.",
        "Noting this is for, same terms as last week.",
    ),
    ("Rate please (#BBS) today", "Rate please today"),
    ("Quote for #BBS and come back to me", "Quote for and come back to me"),
    # Paragraph breaks the desk typed are theirs and survive.
    ("Line one\n\nLine two", "Line one\n\nLine two"),
    # A line that held nothing but a code should not survive as a gap.
    ("#BBS\nPlease quote XOF", "Please quote XOF"),
    ("No codes here at all", "No codes here at all"),
])
def test_removing_a_code_does_not_leave_a_hole(typed, expected) -> None:
    """Stripping takes the spacing with it.

    Found live on 25 October, read in a supplier's group:

        Noting this is for , same terms as last week.

    The code had gone - the part that matters - and what arrived was a
    sentence with a hole in it. A counterparty cannot tell a redaction from a
    broken bot, and the one they will assume is the one that makes them doubt
    the figures in the same message.

    This does not rescue the grammar, and is not meant to: "for, same" is a
    typo where "for , same" is a fault. The desk sees the outgoing text in
    the preview before anything is sent, which is the real safety net, and
    `test_the_preview_is_what_actually_goes_out` is what keeps it honest.
    """
    assert shorthand.strip_codes(typed) == expected


def test_the_preview_is_what_actually_goes_out() -> None:
    """The outbound preview heading must be the heading that is sent.

    The preview composed its body through `outbound_body`, which strips, and
    then put the raw subject above it - so a request raised as

        /npnewsu Please quote XOF 20,100,000 to Senegal for #BBS

    previewed with `#BBS` in the heading and was sent without it. Found on
    25 October.

    The drift is the point rather than the leak: the preview is the desk's
    last chance to see that removing the code left their sentence reading
    oddly, and a preview that is not literally what gets sent cannot do that.
    """
    import ast
    import pathlib

    from app.services import relay

    typed = "Please quote XOF 20,100,000 to Senegal for #BBS"
    assert relay.outward_subject_text(typed) == (
        "Please quote XOF 20,100,000 to Senegal for"
    )

    # And the preview reaches it rather than rendering the subject itself.
    source = pathlib.Path("app/bot/handlers/outbound.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "capture":
            break
    else:
        raise AssertionError("the outbound preview handler has been renamed")

    called = {
        ast.unparse(inner.func)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
    }
    assert "relay.outward_subject_text" in called, (
        "the outbound preview renders the subject itself again, so it can "
        "drift from what the counterparty is sent - it did, until 25 October"
    )


def test_the_confirmation_in_the_counterpartys_room_never_says_the_code() -> None:
    """`/npsetgroupcode` answers in two rooms, and only one may hear the code.

    The command is typed inside the counterparty's own group - deliberately,
    because the room you are standing in is the room being named - so every
    reply it makes is read by that counterparty.

    Found live on 25 October, sitting in a supplier's group since 12:10 that
    same day:

        TEST — Pexi Finance is now #BBS. Writing #BBS in an FX command will
        pick this group without going through the list.

    That is the code, published to the supplier, with instructions for using
    it. Jason, 19 October: "#BBS remains completely internal and must never go
    out to the client/supplier." The refusal path had been fixed hours
    earlier; this, the path that runs every time it *works*, had not.

    Read off the AST rather than by grepping the file, because the fix is a
    comment-heavy one and the words "#BBS" and "is now" appear all over the
    reasoning. What is checked is the actual string literals handed to
    `message.reply` - the ones that land in the counterparty's room.
    """
    import ast
    import pathlib

    source = pathlib.Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "cmd_setgroupcode":
            break
    else:
        raise AssertionError("cmd_setgroupcode has been renamed")

    spoken: list[str] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        target = ast.unparse(call.func)
        if not target.endswith("message.reply"):
            continue
        for argument in call.args:
            # Only literal text. A name here is a variable this test cannot
            # follow, which is exactly the hole the fault came through, so it
            # fails rather than passing on something it did not read.
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                spoken.append(argument.value)
            elif isinstance(argument, ast.JoinedStr):
                parts = [
                    v.value for v in argument.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
                spoken.append("".join(parts))

                # An interpolation is allowed only when it is one of this
                # module's own command-name constants - `/{cmd.REGISTER_CLIENT}`
                # and the like, which say nothing about any group. Anything
                # computed from a code, a title or a domain error is the fault
                # itself, and the guard cannot evaluate it, so it refuses.
                for value in argument.values:
                    if not isinstance(value, ast.FormattedValue):
                        continue
                    written = ast.unparse(value.value)
                    assert written.startswith("cmd."), (
                        f"a reply in the counterparty's room interpolates "
                        f"{written!r}; if that is a code, a group title or a "
                        f"refusal, the counterparty is being told"
                    )
            elif not isinstance(argument, ast.Call):
                raise AssertionError(
                    f"cannot read what is replied in the counterparty's room: "
                    f"{ast.unparse(argument)}"
                )

    assert spoken, "cmd_setgroupcode no longer answers where it was typed"
    for said in spoken:
        assert "#" not in said, f"a code reached the counterparty's room: {said!r}"
        assert "is now" not in said.lower(), (
            f"the counterparty was told what this group is now called: {said!r}"
        )
