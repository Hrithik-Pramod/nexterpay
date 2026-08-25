# Internal test runbook — live Telegram pass

**Internal. Not for the client.**

Do this before NexterPay touch anything. Nothing in the codebase has spoken to
Telegram yet, so this is the first contact. Allow **60–90 minutes** including
setup. Two people is faster than one: one plays the client, one plays staff.

If you only have time for part of it, do sections 1–4. Those cover everything
the client's own test will exercise.

---

## 0 · Before you start (15 min)

Two Telegram accounts. Your own, plus a second one to play the client — a
colleague, or a second account on a spare phone. You cannot properly test this
with one account, because you need to see what the client sees.

**Create the bot**

1. Message @BotFather → `/newbot` → name it `NexterPay Ops (TEST)`.
2. Keep the token safe. It goes in `.env` as `BOT_TOKEN` and nowhere else.
3. `/setjoingroups` → **Enable**.
4. `/setprivacy` → **Enable** (privacy ON). This is the agreed design — the bot
   only sees replies to its own messages. If you turn it off, you are testing a
   different product to the one that was agreed.

**Create two groups**

| Group | Type | Setup |
|---|---|---|
| `TEST — Support Operations` | Supergroup | **Enable Topics** in settings. Add the bot, promote to admin, tick **Manage Topics**. |
| `TEST — Acme Support` | Group | Add the bot. No admin rights needed. |

Add your second account to the Acme group only. It must **not** be in the
Operations group — that separation is half of what you are testing.

**Start it**

```bash
cp .env.example .env          # BOT_TOKEN, DATABASE_URL, ADMIN_BOOTSTRAP_ID
alembic upgrade head
python -m app.bot.main
```

`ADMIN_BOOTSTRAP_ID` is your own Telegram numeric id. Get it by messaging
@userinfobot. Without it you cannot register anything.

**Register**

In the Operations group: `/register_ops support`
In the Acme group: `/register_client support Acme Payments`
In the Operations group, reply to yourself: `/adduser senior_operator support`

**Then run the preflight**

```bash
python scripts/preflight.py
```

Fix anything it reports before going further. It catches the five failures that
otherwise waste the first half hour.

---

## 1 · Raising a request (10 min)

**As the client account, in the Acme group:**

1. Send `/raise`.
2. Press **Raise Request**.
3. Type: `We have not received settlement for 3 March. Can you check?`

**Expect:**

- [ ] Client group: an acknowledgement with a reference like `#1000`, telling
      them to reply to that message
- [ ] Operations group: a new topic named `#1000 · Acme Payments · We have not…`
- [ ] Inside the topic: a header block (client, raised by, department, status,
      priority, owner), the client's message, and an **Actions** keyboard
- [ ] The client sees nothing of the topic

**If the topic is not created**, it is almost always Manage Topics. Re-run the
preflight.

---

## 2 · Working it (15 min)

**As staff, in the topic:**

1. Press **Claim**.
2. Press **Status** → **In Progress**.
3. Press **Priority** → **High**.
4. Type a plain message: `Checking the settlement file with the bank.`
5. Send `/reply we are looking into this and will come back to you shortly.`

**Expect:**

- [ ] Each button posts a line into the topic: `• Claimed by …`,
      `• Status: Claimed → In Progress (…)`, `• Priority: Medium → High (…)`
- [ ] **The header at the top of the topic updates** — owner, status and
      priority change in place. This is the one people forget to check.
- [ ] Your plain message did **not** reach the client
- [ ] The `/reply` text *did* reach the client, prefixed `#1000 —`

> **The critical check.** Look at the client group on the other phone. If
> "Checking the settlement file with the bank" appears there, stop and tell me
> immediately — that is a leak and nothing else matters until it is fixed.

---

## 3 · The client replies (10 min)

**As the client, in the Acme group:**

1. **Reply to** the bot's `#1000 —` message (swipe/right-click → Reply).
2. Send `Attached — sent 3 March at 09:14.` with a photo or PDF attached.

**Expect:**

- [ ] The message and the file appear in the `#1000` topic
- [ ] `• Message received from …` and `• Attachment received from …` appear
- [ ] The file opens correctly from the topic

**Then, the negative case:**

3. Send a fresh message in the group *without* replying: `any news?`

**Expect:**

- [ ] Nothing happens. It does not appear in the topic.

That is correct behaviour under the agreed design, not a bug — but see whether
it feels acceptable in practice, because it is the open question with the
client. Note your impression; it is the most useful thing you will learn today.

**Try a large file** — something over 20 MB. It should relay fine, because we
never download it. Worth proving once.

---

## 4 · Closing (5 min)

1. In the topic, press **History**. Check the trail reads sensibly start to
   finish and names the right people.
2. Press **Close**.

**Expect:**

- [ ] `• Closed by …` in the topic
- [ ] Header shows `Status: Closed`
- [ ] The topic is closed/archived in the group
- [ ] The client receives a closure message

---

## 5 · Permissions (10 min)

Needs a second staff account. Add them as `operator`:

- [ ] As the operator, press **Priority** → refused with a message about needing
      Senior Operator
- [ ] As the operator, **Claim** an unclaimed item → allowed
- [ ] Remove them with `/removeuser` (as a reply), then have them press any
      button → refused

---

## 6 · Multiple items (10 min)

The scenario that breaks naive implementations.

1. As the client, raise **three** requests in the same group.
2. Reply to the *first* acknowledgement with `this one is urgent`.

**Expect:**

- [ ] Three separate topics
- [ ] The reply lands in the **first** topic, not the newest

---

## 7 · Resilience (5 min)

1. Stop the bot (Ctrl-C).
2. As the client, send a reply to an acknowledgement.
3. Start the bot again.

**Expect:**

- [ ] The message is picked up on restart and appears in the topic

Telegram queues updates for a bot that is offline, so nothing should be lost.
Worth proving, since it is the honest answer to "what if it goes down".

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No topic created | Topics off, or no Manage Topics | Enable both, re-run preflight |
| Bot silent in a group | Group not registered | `/register_ops` or `/register_client` |
| Buttons say "not registered for this department" | Staff record missing, or wrong department | `/adduser <role> <department>` |
| Client message ignored | They typed instead of replying | Expected. See section 3. |
| `/register_ops` does nothing | You are not the bootstrap admin | Set `ADMIN_BOOTSTRAP_ID` to your Telegram id, restart |
| Duplicate messages | Two bot processes running | Kill one. Only ever run one. |
| Nothing at all happens | Bot not started, or wrong token | Check the console output |

---

## What to record

For each section, note pass or fail and anything that felt awkward rather than
broken. The awkward things are what to fix before the client sees it; the
broken things are what to fix before anyone sees it.

Send me: the section number, what you expected, what happened, and a screenshot
of both groups where it matters.
