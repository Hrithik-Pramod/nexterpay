#!/usr/bin/env bash
#
# Shared by backup.sh and restore.sh. Sourced, never executed.
#
# One function lives here, and it exists because the obvious one-liner is
# wrong in a way that is invisible until the day it matters.

# Does this gzipped file contain a NexterPay schema?
#
# The obvious way to write this is:
#
#     zcat "$file" | grep -q "CREATE TABLE public.work_items"
#
# and it is a trap. `grep -q` stops reading and exits 0 the moment it finds a
# match. That closes the pipe while `zcat` is still writing, so zcat is killed
# by SIGPIPE and exits 141. Under `set -o pipefail` - which both callers use,
# correctly - the pipeline takes zcat's status, so a dump that DOES contain
# the table reports failure.
#
# Whether it fires depends on whether zcat finished writing before grep quit,
# which depends on the size of the dump against the 64KB pipe buffer. So it
# passed every test on an empty UAT database and began failing once NexterPay
# had real requests in it: a backup script that worked while the data was
# worthless and refused the moment the data mattered. It cost a deploy on
# 4 September, and the same line in restore.sh would have refused a perfectly
# good backup during an actual recovery, which is where it would have been
# unforgivable.
#
# `grep -c` counts, so it must read the whole stream to the end. Nothing is
# left writing into a closed pipe, and the exit status is honest.
looks_like_a_nexterpay_dump() {
  local file="$1" matches
  matches="$(zcat "$file" | grep -c "CREATE TABLE public.work_items" || true)"
  [[ "${matches:-0}" -gt 0 ]]
}
