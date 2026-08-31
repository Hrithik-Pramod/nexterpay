#!/usr/bin/env bash
#
# Restore the database from a backup.
#
#     bash scripts/restore.sh ~/backups/nexterpay-2026-08-30-0200.sql.gz
#
# This overwrites everything currently in the database. It therefore asks you
# to type the word RESTORE rather than accepting a y/n, which is too easy to
# hit by reflex, and it takes a safety copy of the current state first so a
# restore of the wrong file is survivable.
#
# The bot is stopped for the duration. Restoring underneath a running bot
# would have it writing rows into a database being replaced around it.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-${HOME}/backups}"

cd "$PROJECT_DIR"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && . ./.env && set +a
DB_USER="${POSTGRES_USER:-nexterpay}"
DB_NAME="${POSTGRES_DB:-nexterpay_ops}"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
die() { printf "\033[1;31mRESTORE FAILED:\033[0m %s\n" "$*" >&2; exit 1; }

FILE="${1:-}"
if [[ -z "$FILE" ]]; then
  echo "Usage: bash scripts/restore.sh <backup.sql.gz>"
  echo
  echo "Available:"
  ls -1t "${BACKUP_DIR}"/nexterpay-*.sql.gz 2>/dev/null | sed 's/^/  /' || echo "  (none)"
  exit 2
fi

[[ -f "$FILE" ]] || die "no such file: $FILE"
gzip -t "$FILE" 2>/dev/null || die "$FILE is not a readable gzip file"
zcat "$FILE" | grep -q "CREATE TABLE public.work_items" \
  || die "$FILE does not look like a NexterPay dump"

say "About to restore ${DB_NAME} from:"
echo "    $FILE"
echo "    $(stat -c %s "$FILE") bytes, taken $(date -d "@$(stat -c %Y "$FILE")" '+%d %b %Y %H:%M')"
echo
echo "  Everything currently in the database will be replaced."
echo
read -r -p "  Type RESTORE to continue: " confirm
[[ "$confirm" == "RESTORE" ]] || { echo "Cancelled."; exit 1; }

# A restore of the wrong file is a normal mistake. Make it recoverable.
say "Taking a safety copy of the current database first"
mkdir -p "$BACKUP_DIR"
safety="${BACKUP_DIR}/pre-restore-$(date +%F-%H%M%S).sql.gz"
docker compose exec -T db pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" \
  | gzip > "$safety" || die "could not take a safety copy - stopping here"
echo "    $safety"

say "Stopping the bot"
docker compose stop bot >/dev/null

say "Restoring"
if ! zcat "$FILE" | docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" \
     -v ON_ERROR_STOP=1 >/tmp/restore.out 2>&1; then
  tail -20 /tmp/restore.out >&2
  say "Restore failed. The bot is still stopped. Your previous data is in:"
  echo "    $safety"
  exit 1
fi

say "Starting the bot"
docker compose start bot >/dev/null

say "Done. Check it came back correctly:"
echo "    docker compose exec bot python scripts/preflight.py"
echo
echo "  Safety copy of the pre-restore state kept at:"
echo "    $safety"
