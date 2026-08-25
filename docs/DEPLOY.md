# Deploying the test server

Written for someone who has not set up a server before. Follow it in order and
the bot will be running in about 40 minutes, most of which is waiting.

Target: **Hetzner Cloud, Falkenstein or Helsinki**, Ubuntu 24.04, running the
bot, Postgres and Redis together. Around **€4.35/month** for the CX22, billed
hourly, so a week of testing costs about a pound.

> **Scope.** This is a test server for UAT. It is not the production
> deployment — that decision, whose cloud account it lives in, is still open
> with NexterPay. Use a **separate test bot** from BotFather so the eventual
> production token never touches this machine.

---

## Read this first: Hetzner has no UK location

Hetzner Cloud runs from six places: **Falkenstein** and **Nuremberg**
(Germany), **Helsinki** (Finland), Ashburn and Hillsboro (USA), and Singapore.
There is no London.

For a UAT server with invented test data this does not matter — Germany and
Finland are inside the EU, Hetzner owns those data centres outright, and the
company is subject to GDPR.

It does matter for **production**, and it is worth putting to NexterPay before
anyone assumes this box becomes the live one. If they turn out to need UK
residency for client settlement data, Hetzner cannot host it and you would move
to a provider with a London region. That is a five-minute conversation now and
a migration later.

**Choose Falkenstein (`fsn1`)** unless you have a reason not to. It is
Hetzner's largest site and has the best availability of server types.

---

## What you are building

```
     Telegram  ←──── outbound only ────  [ your server ]
                                            ├── bot        (aiogram)
                                            ├── postgres   (the data)
                                            └── redis      (send queue)
```

The bot dials out to Telegram. Nothing dials in. No domain name, no SSL
certificate, no open ports except SSH. That is why this is a 40-minute job
rather than a day.

---

## 1 · Create a Hetzner account (10 min)

1. Go to **https://console.hetzner.cloud** and sign up.
2. Verify your email, then add a payment card.
3. New accounts are sometimes asked for ID verification. If that happens it can
   take a few hours — annoying if you are trying to finish today, so start this
   step first and do the group setup while you wait.

---

## 2 · Create the server (5 min)

In the Hetzner Cloud Console:

1. **New Project** → call it `NexterPay`.
2. **Add Server**.

| Setting | Choose |
|---|---|
| Location | **Falkenstein** (or Helsinki) |
| Image | **Ubuntu 24.04** |
| Type | **Shared vCPU → x86 → CX22** (2 vCPU, 4 GB, 40 GB) |
| Networking | IPv4 + IPv6, leave as default |
| SSH key | **Add your public key** — see below |
| Name | `nexterpay-test` |

Confirm the price shown at checkout; Hetzner adjusted some plans during 2026
and the figure above may have moved.

CX22's 4 GB is comfortable for Postgres, Redis and the bot together. Do not
drop to a 2 GB plan to save a euro — the way you discover you were short is the
database being killed mid-test, which looks like a mystery bug.

### Your SSH key

On Windows, in PowerShell:

```powershell
ssh-keygen -t ed25519 -C "nexterpay"
# press Enter three times to accept the defaults
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

Copy the whole line it prints — it starts `ssh-ed25519` — and paste it into
Hetzner's SSH key box.

> Use a key, not a password. A password-accessible server on a public IP gets
> found and attacked within hours. That is not caution, it is just what
> automated scanners do all day.

Click **Create & Buy Now**. Ready in about 30 seconds. Note the IPv4 address.

---

## 3 · Prepare the server (5 min)

From PowerShell on your machine:

```powershell
ssh root@YOUR_SERVER_IP
```

Say `yes` to the fingerprint prompt. You are now on the server.

Run the bootstrap script. It installs Docker, creates a non-root user, sets up
a firewall, disables password logins and turns on automatic security updates:

```bash
curl -fsSL -o bootstrap.sh \
  https://raw.githubusercontent.com/YOUR_ORG/nexterpay-ops/main/scripts/bootstrap-server.sh
bash bootstrap.sh
```

**No repository yet?** Paste the file across instead:

```bash
nano bootstrap-server.sh
# paste the contents, then Ctrl+O, Enter, Ctrl+X
bash bootstrap-server.sh
```

When it finishes, log back in as the application user:

```bash
exit
ssh nexterpay@YOUR_SERVER_IP
```

---

## 4 · Get the code onto the server (5 min)

**If you have a Git repository:**

```bash
git clone https://github.com/YOUR_ORG/nexterpay-ops.git
cd nexterpay-ops
```

**If you do not**, copy it from your machine. In PowerShell, from the folder
*above* the project:

```powershell
scp -r C:\Users\hrith\Nexterpay\nexterpay-ops nexterpay@YOUR_SERVER_IP:~/
```

Then on the server: `cd ~/nexterpay-ops`

> Getting this into Git today is worth twenty minutes. Deploying an update
> becomes `git pull && docker compose up -d --build` rather than copying
> folders over SCP, and you will be deploying updates all week.

---

## 5 · Configure it (5 min)

```bash
cp .env.example .env
nano .env
```

Fill in four things:

```ini
BOT_TOKEN=8123456789:AAF...        # from BotFather, the TEST bot
POSTGRES_PASSWORD=                 # see below
ADMIN_BOOTSTRAP_ID=123456789       # your Telegram numeric id
LOG_LEVEL=INFO
```

Generate the database password on the server rather than inventing one:

```bash
openssl rand -base64 24
```

Your Telegram id comes from messaging **@userinfobot**. Without it you cannot
register any groups.

Save with `Ctrl+O`, `Enter`, `Ctrl+X`, then:

```bash
chmod 600 .env    # nobody else on the box should be able to read the token
```

---

## 6 · Start it (5 min)

```bash
docker compose up -d --build
```

The first run pulls images and builds — two or three minutes. Then:

```bash
docker compose ps          # db and redis healthy, migrate exited 0, bot running
docker compose logs -f bot
```

You want to see:

```
Reply routing strategy: reply_to_ack
Starting as @YourTestBot
```

`Ctrl+C` stops following the logs. It does not stop the bot.

---

## 7 · Set up the Telegram groups (10 min)

Follow **`docs/INTERNAL_TEST_RUNBOOK.md`** section 0 for group creation, then
register them by sending these *inside the groups themselves*:

```
/register_ops support                          ← in the Operations group
/register_client support Acme Payments         ← in the client group
/adduser senior_operator support               ← as a reply to yourself
```

Then confirm everything before anyone tests:

```bash
docker compose exec bot python scripts/preflight.py
```

Fix whatever it reports. It catches the five setup mistakes that otherwise eat
your first half hour.

---

## Running it day to day

| What | Command |
|---|---|
| See what is running | `docker compose ps` |
| Watch the logs | `docker compose logs -f bot` |
| Last 100 log lines | `docker compose logs --tail=100 bot` |
| Restart the bot | `docker compose restart bot` |
| Deploy an update | `git pull && docker compose up -d --build` |
| Stop everything | `docker compose down` |
| Re-check permissions | `docker compose exec bot python scripts/preflight.py` |
| Database shell | `docker compose exec db psql -U nexterpay nexterpay_ops` |
| Free memory | `free -h` |

### Back up the database

Not critical for UAT, but it is one command:

```bash
docker compose exec -T db pg_dump -U nexterpay nexterpay_ops \
  | gzip > ~/backups/nexterpay-$(date +%F-%H%M).sql.gz
```

Nightly at 2am — `crontab -e`, then:

```
0 2 * * * cd ~/nexterpay-ops && docker compose exec -T db pg_dump -U nexterpay nexterpay_ops | gzip > ~/backups/nexterpay-$(date +\%F).sql.gz
```

### Wipe test data before the pilot

UAT fills the database with nonsense. Start clean afterwards:

```bash
docker compose down -v      # -v removes the data volume too
docker compose up -d
```

---

## If something goes wrong

| Symptom | Likely cause | What to do |
|---|---|---|
| `bot` keeps restarting | Bad token, or database not up | `docker compose logs bot` |
| `Unauthorized` in the logs | Token wrong or revoked | Check `.env`, get a fresh token from BotFather |
| `migrate` exited non-zero | Database was not ready yet | `docker compose up -d` again; it retries |
| Bot silent in a group | Group not registered | `/register_ops` or `/register_client` |
| No topics created | Topics off, or no Manage Topics | Run `preflight.py`; it names the problem |
| Messages arriving twice | Two bot processes | Only ever run one. `docker compose ps` |
| Container killed unexpectedly | Out of memory | `free -h`; rescale in the console |
| Cannot SSH in | Password login now disabled | Use your key. Hetzner's web Console gives emergency access |

For anything else, start with `docker compose logs --tail=200 bot`. The logs
name the chat and the work item, so they are usually enough.

### Rescaling

Hetzner can rescale in place: power off, **Rescaling**, choose a larger type,
power on. Picking "upgrade CPU and RAM only" keeps the disk, and is reversible.
A rescale that also grows the disk cannot be undone.

---

## Cost and cleanup

Around €4.35/month, billed hourly, so a week is roughly a pound.

When UAT is finished, **delete the server** in the console rather than leaving
it idle. It holds test data and a bot token and there is no reason to keep
either lying around. Note Hetzner's rule: *a powered-off server is still
billed.* Only deleting it stops the charge.

---

## Before this becomes production

Fine for UAT, not sufficient for live client data:

- No off-site backup of the database.
- No monitoring or alerting if the bot stops.
- It is in your account, not NexterPay's — an open question with them, and the
  answer probably ought to be theirs.
- The bot token is a test one, which is correct now and must be replaced.
- **It is in Germany or Finland, not the UK.** If NexterPay require UK
  residency, production needs a different provider.

Worth raising once UAT passes, rather than letting this quietly become the
live system.
