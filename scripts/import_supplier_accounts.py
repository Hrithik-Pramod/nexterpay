"""Load a supplier's account list.

NexterPay keep this in a spreadsheet. The one sent on 4 October looks like
this, and the shape is theirs rather than ours:

    ,Client,,
    BBS 2,1Win,,
    BBS 4,Ace,,
    BBD 13,Dalapay/Captainsbet,,

Two columns that matter and two empty ones, a header row that is not quite a
header, an account prefix on every line, and at least one typo - `BBD 13`,
which Jason confirmed is `BBS 13`. All of that is read rather than corrected
in the file, because the file is theirs and will be sent again.

Run it:

    python -m scripts.import_supplier_accounts ClientBalances.csv --supplier BBS
    python -m scripts.import_supplier_accounts ClientBalances.csv --supplier BBS --confirm

Dry run by default. It prints what it would load, which clients it can match
to ones registered here, and which it cannot - and loads nothing until
`--confirm`.

Re-running is safe and expected. The list changes, and a reload re-links every
row, so a client registered after the first load starts resolving without the
file having to be sent again.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.base import init_engine, session_scope
from app.db.models import Client
from app.domain import accounts

# The prefixes seen in their file. `BBD` is a typo for `BBS`, confirmed by
# Jason on 4 October; it is corrected on the way in rather than in their
# spreadsheet, which is not ours to edit.
TYPOS = {"BBD": "BBS"}


def read_rows(path: Path, supplier: str) -> list[tuple[str, str, str]]:
    """(supplier code, account number, client name) from their spreadsheet.

    Rows with no account or no client are skipped in silence - the file has
    blank lines at the end and a header that is not quite one, and neither is
    worth a warning every time the list is reloaded.
    """
    found = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2:
                continue
            account, client = row[0].strip(), row[1].strip()
            if not account or not client or client.lower() == "client":
                continue

            # "BBS 2" carries its own supplier; a bare "2" takes the one asked
            # for on the command line.
            parts = account.split()
            if len(parts) >= 2 and parts[0].isalpha():
                code = TYPOS.get(parts[0].upper(), parts[0].upper())
                number = " ".join(parts[1:])
            else:
                code, number = supplier.upper(), account

            found.append((code, accounts.normalise_number(number), client))
    return found


async def run(path: Path, supplier: str, confirm: bool) -> int:
    rows = read_rows(path, supplier)
    if not rows:
        print(f"Nothing to load from {path}.")
        return 1

    # The URL, like every other script here passes it.
    #
    # This read `init_engine()` with no argument, and `init_engine` has
    # required one since it was written - so this script had never once been
    # run. It was delivered on 4 October, tested at the parsing level, and
    # went into the instructions as the way to load NexterPay's account list.
    # The first time anybody tried it, on 26 October, it raised before
    # touching the database:
    #
    #     TypeError: init_engine() missing 1 required positional argument
    #
    # `test_no_script_is_wired_wrong` in test_shell_scripts.py now checks the
    # call in every script under scripts/, because a script nobody has run is
    # indistinguishable from one that works until the day it is needed.
    init_engine(get_settings().database_url)
    async with session_scope() as session:
        known = {
            name for name in (
                await session.execute(select(Client.name))
            ).scalars().all()
        }

        matched = [r for r in rows if r[2] in known]
        unmatched = [r for r in rows if r[2] not in known]

        suppliers = sorted({r[0] for r in rows})
        print(f"{len(rows)} account rows across {', '.join(suppliers)}")
        print(f"  {len(matched)} name a client registered here")
        print(f"  {len(unmatched)} do not, and will be kept unresolved")

        shared = {}
        for code, number, name in rows:
            shared.setdefault((code, number), []).append(name)
        several = {k: v for k, v in shared.items() if len(v) > 1}
        if several:
            print(f"\n  {len(several)} account(s) carry more than one client:")
            for (code, number), names in sorted(several.items()):
                print(f"    {code} {number}: {', '.join(sorted(names))}")
            print("  Those narrow a payment rather than identifying it.")

        if unmatched:
            print("\n  Not registered here yet:")
            for _, _, name in sorted(set(unmatched), key=lambda r: r[2]):
                print(f"    {name}")

        if not confirm:
            print("\nDry run. Nothing was written. Add --confirm to load it.")
            return 0

        # Counted as stored, not as read.
        #
        # This printed `len(rows)`, which is how many lines the file had, and
        # said "Loaded" about it. `accounts.record` is an upsert, so a line
        # repeated in the spreadsheet - their 4 October file lists
        # `BBS 2,1Win` twice - becomes one row.
        #
        # Run live on 26 October it reported "Loaded 19 rows" against a table
        # holding 18, and both numbers were right about different things.
        # Somebody reconciling a 20-line file against that would go looking
        # for a row that was never missing. The dedupe is correct; only the
        # sentence was wrong.
        distinct = {(code, number, name) for code, number, name in rows}
        for code, number, name in sorted(distinct):
            await accounts.record(
                session, supplier_code=code, number=number, client_name=name
            )

        print("\n" + loaded_message(read=len(rows), stored=len(distinct)))
    return 0


def loaded_message(*, read: int, stored: int) -> str:
    """What to say afterwards, counted as rows stored rather than lines read."""
    said = f"Loaded {stored} rows."
    repeated = read - stored
    if repeated:
        said += (
            f" {repeated} line(s) in the file repeat one already counted, so "
            f"they are the same row."
        )
    return said


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--supplier", default="BBS",
        help="the supplier these accounts belong to, when the file does not say",
    )
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"No such file: {args.path}")
        return 1
    return asyncio.run(run(args.path, args.supplier, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
