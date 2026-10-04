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

    init_engine()
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

        for code, number, name in rows:
            await accounts.record(
                session, supplier_code=code, number=number, client_name=name
            )
        print(f"\nLoaded {len(rows)} rows.")
    return 0


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
