# NexterPay Operations Platform — Phase 1

Clients keep using their existing Telegram groups. NexterPay staff work from
centralised departmental Operations Groups. The bot moves messages between the
two and turns every request into a tracked work item.

## Status

Phase 1 is code-complete. 55 tests, all passing, none of which need a Telegram
token. What has **not** happened is a run against real Telegram — see
[Before go-live](#before-go-live).

```
app/
  config.py              settings from environment
  db/
    base.py              engine, session scope, declarative base
    models.py            clients, chats, staff, work_items, messages,
                         attachments, events
  domain/                no Telegram imports anywhere in here
    enums.py             statuses, priorities, roles, event types
    errors.py            domain failures the bot layer translates
    work_items.py        lifecycle — the only place work items change
    history.py           event rendering and history reconstruction
  services/
    gateway.py           every Bot API call, behind one interface
    throttle.py          rate limiting and 429 handling
    relay.py             topics, two-way relay, attachments
  bot/
    main.py              entrypoint, router wiring
    deps.py              chat/staff resolution for handlers
    keyboards.py         inline keyboards and callback payloads
    registry.py          onboarding groups and staff
    routing.py           reply routing strategies
    handlers/
      client.py          Raise Request, client replies
      staff.py           actions, /np_reply, /np_note, /np_history, /np_assign
      admin.py           registration, user management, /np_workload
scripts/smoke.py         full walkthrough, prints both sides
tests/                   55 tests
```

## Coverage against the PRD

Checked against the Phase 1 inclusions in §16 and the requirements they refer
back to.

| PRD | Requirement | Status |
|---|---|---|
| §16 | Existing client groups retained | Done |
| §16, §6 | Four departmental Operations Groups | Done |
| §16, §5 | Bot installed in each client group | Done — `/np_register_client` |
| §8 | Raise Request workflow | Done |
| §15.4 | Commercial Enquiry workflow | Wording and free-text capture done; the separate stage model is **not** built — see below |
| §16 | Automatic Work Item creation | Done |
| §9 | A Telegram topic per Work Item | Done, including archive on close |
| §7.3 | Ownership and assignment | Done — topic header is edited in place so the current owner, status and priority are always visible |
| §3.5, §4, §10.2, §15 | The documented flow diagrams | Done — each is executed step by step in `tests/test_prd_flows.py` |
| §8.2 | Routing by source group, all four departments | Done |
| §7.4 | Internal notes | Done, and guarded by tests |
| §10.4 | Two-way message synchronisation | Done |
| §7.5 | Attachments both directions | Done — client→topic and staff→client |
| §11 | All nine statuses | Done |
| §12 | All four priorities | Done |
| §7.2 | All fourteen Work Item fields | Done |
| §13 | Five permission tiers | Done |
| §3.7, §7 | Complete audit logging | Done, append-only |
| §14 | Operational automation | Done |
| §3.6 | Ownership notifications | Done — mention in topic on reassignment |

### Not built, and why

**Business Operations stage model (§15.4).** The PRD defines two competing
progressions: the common status list in §11, and Qualification → Commercial
Discussion → Proposal → Awaiting Client → Agreed/Declined in §15.4. Business
currently uses the §11 statuses. Building both would mean two workflows to
maintain, so this waits on NexterPay choosing one.

**Escalation target (§11).** `Escalated` exists as a status and is restricted
to Senior Operator and above. Who it escalates *to*, and what should then
happen, is undefined in the PRD.

**Reopening on client follow-up (§B2).** `reopen()` exists and requires a
Manager. Whether a client following up on a closed request should reopen it or
create a new one has not been decided, so nothing calls it automatically.

**Administrative configuration (§13).** Departments, users and permissions are
managed by command. Operational settings are environment variables, changed by
redeploying rather than by a command.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # BOT_TOKEN, DATABASE_URL, ADMIN_BOOTSTRAP_ID
alembic upgrade head
pytest
python scripts/smoke.py       # see it work without a token

python -m app.bot.main
```

Deployment is `docker-compose up -d`. One bot container, always — see the note
in `docker-compose.yml`.

## Setting up groups

1. Create a supergroup per department for internal use, **enable topics**, add
   the bot as an administrator with "manage topics".
2. In that group: `/np_register_ops support`
3. Add the bot to a client group, then: `/np_register_client support Acme Payments`
4. Add staff, replying to the person: `/np_adduser operator support`

`ADMIN_BOOTSTRAP_ID` lets the first administrator act before any exist in the
database. Clear it once a real administrator is registered.

## Commands

**Client groups** — `/np_raise` (or `/np_enquiry` in Business groups) offers the
button. Everything after that is ordinary conversation.

**Operations Groups**

| Command | Effect |
|---|---|
| `/np_reply <text>` | Sends to the client. The only thing that does. |
| `/np_note <text>` | Internal note. Never leaves the group. |
| `/np_history` | Full audit trail for the topic's work item. |
| `/np_assign` | Reply to someone, then assign to them. |
| `/np_workload` | Open items for the department. |
| `/np_whoami` | Your registered role. |

Claim, status, priority, history and close are also inline buttons on the work
item header.

## Decisions this code encodes

From the requirements exchange with NexterPay. Recorded here because they are
not obvious from the code.

**Bot-only.** Nothing but the bot reads the data. No admin interface, no
reporting, no export. NexterPay keep a passive Telegram account in the groups
and review history by reading it.

**So every state change is posted into the topic.** Ownership, status and
priority changes appear as visible lines, not just database rows — otherwise a
reviewer sees conversation with no record of who acted.
`test_every_event_type_has_a_renderer` fails if an event type is added without
a renderer. That is deliberate.

**Reply routing is reply-to-acknowledgement.** The bot posts a message carrying
the reference; the client replies to it; `reply_to_message` resolves the work
item. A freshly typed message resolves to nothing and is logged, not guessed
at. The strategy is behind an interface so this can change in one binding.

**Outbound is explicit.** `/np_reply` sends to a client. Plain typing in a topic is
an internal note. There is no setting that changes this, and
`test_internal_note_never_reaches_the_client` guards it.

**Nothing changes a work item without an event.** `events` is append-only and
`app/domain/work_items.py` is the only writer.

**We keep what Telegram loses.** The Bot API never reports deletions, so a
client deleting a message removes it from their group but not from `messages`.

**Attachments relay by `file_id`.** Never downloaded, so the 20 MB `getFile`
ceiling does not apply — a 40 MB statement passes through fine. If NexterPay
later want their own archived copy, that cap becomes real (question D3).

## Before go-live

Nothing here has spoken to Telegram. The following need a live run:

- Topic creation and archiving in a real forum supergroup
- `file_id` relay across chats, both directions
- Callback buttons under real permissions
- Behaviour when the bot lacks `can_manage_topics`
- Rate limits under genuine load — `scripts/smoke.py` cannot prove this

Allow half a day with a throwaway bot and two test groups.

## Open with the client

Behaviour, not structure — none of these change the shape of the code:

- Does the bot get administrator rights in client groups, or see only what is
  addressed to it? Currently assumes the latter.
- What should happen when a client types instead of replying? Currently logged
  and ignored.
- Is the previous NexterPay bot still in the client groups? Two bots in one
  group cannot see each other's messages.
- Volumes — clients, groups, messages per day.
- Does Business Operations use its own stage model or the common statuses?
  Currently the common statuses.
- Should closing a request notify the client? Currently yes,
  `NOTIFY_CLIENT_ON_CLOSE`.

## Constraints worth remembering

- ~1 message/second per chat, ~20/minute per group, ~30/second overall. The
  Operations Groups are the bottleneck, not the client groups.
- `getFile` caps downloads at 20 MB. Relaying by `file_id` has no such limit.
- Bots cannot see messages sent by other bots.
- Forum topics need a supergroup with topics enabled and `can_manage_topics`.
- Message deletions are never reported to a bot.
- One bot process only, under long polling.
