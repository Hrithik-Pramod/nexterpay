# Pushing this to GitHub

Run these on your own machine — pushing needs your GitHub credentials.

Repository: `https://github.com/Hrithik-Pramod/nexterpay.git`

---

## First, delete the partial `.git` folder

A half-initialised `.git` was created and could not be cleaned up, so remove it
before starting. In PowerShell:

```powershell
cd C:\Users\hrith\Nexterpay\nexterpay-ops
Remove-Item -Recurse -Force .git
```

If Explorer is open in that folder, close it first or the delete may be
blocked.

---

## Then push

```powershell
cd C:\Users\hrith\Nexterpay\nexterpay-ops

git init
git branch -M main
git add -A
git status                      # look at this before committing
git commit -m "Phase 1: Telegram operations platform"

git remote add origin https://github.com/Hrithik-Pramod/nexterpay.git
git push -u origin main
```

`git status` before the commit is worth thirty seconds. You are checking that
**`.env` is not listed**. It is in `.gitignore`, but a bot token in Git history
means revoking the token, and it is far easier to catch here.

If the repository already has a commit (a README created at setup), the push
will be rejected. Then:

```powershell
git pull --rebase origin main
git push -u origin main
```

---

## If it asks for a password

GitHub stopped accepting account passwords over HTTPS. Use a Personal Access
Token instead:

1. GitHub → Settings → Developer settings → Personal access tokens →
   **Tokens (classic)** → Generate new token
2. Tick the **repo** scope, generate it, copy it
3. When git asks for a password, paste the token

Git Credential Manager ships with Git for Windows and will remember it, so this
is a one-time step. If a browser window opens instead, sign in there and you
are done.

---

## Make it private

This repository will hold a client's operational platform. If it is currently
public:

**Settings → General → Danger Zone → Change repository visibility → Private**

Worth doing before the first push rather than after.

---

## What you get on the first push

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs automatically:

- `ruff check` — lint
- `pytest` — all 80 tests
- Migrations applied, checked for drift, reversed, reapplied
- `scripts/smoke.py` — the full end-to-end walkthrough
- A scan that fails the build if a bot token or `.env` is ever committed

That last one matters more than it looks. The most likely way this project
leaks a credential is somebody committing `.env` in a hurry during UAT week.
The check catches it at the push rather than at the incident.

---

## Then deploying gets easier

Once this is on GitHub, `docs/DEPLOY.md` step 4 becomes:

```bash
git clone https://github.com/Hrithik-Pramod/nexterpay.git
cd nexterpay
```

and every later update on the server is:

```bash
git pull && docker compose up -d --build
```

rather than copying folders over SCP each time. You will be doing this several
times during UAT week.
