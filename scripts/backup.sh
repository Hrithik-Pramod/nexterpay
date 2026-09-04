#!/usr/bin/env bash
#
# Take a database backup, prove it is a real one, and rotate old ones.
#
#     bash scripts/backup.sh            # take a backup
#     bash scripts/backup.sh --check    # is there a recent, valid backup?
#
# Designed to be run from cron. Two things it deliberately does not do:
#
#   * It never reports success on an empty or truncated file. A backup you
#     have never restored is a guess; a backup that is zero bytes is a lie.
#     The dump is written to a temporary name, verified, and only then moved
#     into place, so a failed run cannot leave something that looks like a
#     backup behind.
#
#   * It never deletes the most recent backups, whatever their age. The naive
#     "delete anything older than N days" removes every backup you have if
#     backups stopped running N+1 days ago - which is exactly the moment you
#     need them.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-${HOME}/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
KEEP_ALWAYS="${KEEP_ALWAYS:-7}"     # newest N are never deleted, whatever their age
MIN_BYTES="${MIN_BYTES:-800}"       # backstop against truncation, not a size expectation
STALE_HOURS="${STALE_HOURS:-30}"    # --check fails if the newest is older than this

cd "$PROJECT_DIR"

# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/lib-dump.sh"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && . ./.env && set +a
DB_USER="${POSTGRES_USER:-nexterpay}"
DB_NAME="${POSTGRES_DB:-nexterpay_ops}"

say()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!!\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[1;31mBACKUP FAILED:\033[0m %s\n" "$*" >&2; exit 1; }

# Written without `| head -1` on purpose. `head` closing the pipe under a
# still-writing `ls` is the same SIGPIPE-plus-pipefail trap that broke the
# dump check - harmless at seven backups, which is exactly what was said
# about the other one. Reading the list into an array reads all of it.
newest() {
  local files=()
  mapfile -t files < <(ls -1t "${BACKUP_DIR}"/nexterpay-*.sql.gz 2>/dev/null || true)
  (( ${#files[@]} )) || return 1
  printf '%s\n' "${files[0]}"
}

# --------------------------------------------------------------------------
# --check: is there a recent, readable backup? For cron alerting and for
# answering "are we actually protected?" without trusting the crontab.
# --------------------------------------------------------------------------
if [[ "${1:-}" == "--check" ]]; then
  latest="$(newest || true)"
  [[ -n "$latest" ]] || die "no backups found in ${BACKUP_DIR}"

  age_h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
  size=$(stat -c %s "$latest")

  gzip -t "$latest" 2>/dev/null || die "newest backup is corrupt: $latest"
  (( size >= MIN_BYTES )) || die "newest backup is only ${size} bytes: $latest"
  (( age_h <= STALE_HOURS )) || die "newest backup is ${age_h}h old: $latest"

  say "OK - $(basename "$latest"), ${size} bytes, ${age_h}h old"
  ls -1t "${BACKUP_DIR}"/nexterpay-*.sql.gz | wc -l | xargs printf "    %s backups retained\n"
  exit 0
fi

# --------------------------------------------------------------------------
# Take the backup
# --------------------------------------------------------------------------
mkdir -p "$BACKUP_DIR"

stamp="$(date +%F-%H%M)"
final="${BACKUP_DIR}/nexterpay-${stamp}.sql.gz"
temp="${final}.partial"
trap 'rm -f "$temp"' EXIT

say "Dumping ${DB_NAME} from the db container"

# --clean --if-exists makes the dump safe to restore over an existing
# database, which is the situation you are actually in when restoring.
if ! docker compose exec -T db \
      pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" 2>/tmp/pg_dump.err \
      | gzip > "$temp"; then
  warn "$(cat /tmp/pg_dump.err 2>/dev/null || true)"
  die "pg_dump did not complete - is the db container running?"
fi

# `docker compose exec` can exit 0 while producing nothing useful, so check
# the artefact rather than the exit code.
size=$(stat -c %s "$temp" 2>/dev/null || echo 0)
(( size >= MIN_BYTES )) || die "dump is only ${size} bytes - refusing to keep it"
gzip -t "$temp" 2>/dev/null || die "dump did not gzip cleanly"
looks_like_a_nexterpay_dump "$temp" \
  || die "dump does not contain the work_items table - wrong database?"

mv "$temp" "$final"
trap - EXIT
say "Wrote $(basename "$final") (${size} bytes)"

# --------------------------------------------------------------------------
# Rotate, keeping the newest KEEP_ALWAYS no matter how old they are
# --------------------------------------------------------------------------
mapfile -t all < <(ls -1t "${BACKUP_DIR}"/nexterpay-*.sql.gz 2>/dev/null || true)
if (( ${#all[@]} > KEEP_ALWAYS )); then
  for old in "${all[@]:KEEP_ALWAYS}"; do
    if [[ -n "$(find "$old" -mtime "+${KEEP_DAYS}" 2>/dev/null)" ]]; then
      rm -f "$old"
      echo "    removed $(basename "$old")"
    fi
  done
fi

say "Done. ${#all[@]} backup(s) in ${BACKUP_DIR}"
