"""Our accounts with a supplier, and whose business runs through each one.

NexterPay, through Jason on 4 October: "BBS is a big supplier, so we have
multiple accounts. BBS number is Nexterpay Number." Every settlement line they
send ends with one:

    CI - 50250000/583=86,192.11   (07/09/2026) Nexterpay 5

**What this is worth, precisely.** The account number narrows a payment down.
It does not identify it. BBS 1 carries both LuckyStar and Spayz Category B, so
"Nexterpay 1" means one of two clients rather than one client - and anything
built on the stronger reading would be wrong for exactly the busiest accounts.

That still matters a great deal. Settlement lines carry no reference, so the
only thing to match on was the amount, and two clients sending fifty million
XOF on the same day is ordinary on a desk doing volume. Amount *and* account
is far more selective, so most of the cases this platform currently refuses to
guess on will resolve. Where two candidates survive both filters it still
refuses, which is the behaviour that was right before and is right now.

Numbering is per supplier - BBS 5 and SPEX 5 are unrelated - so nothing here
takes a number without the supplier beside it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, SupplierAccount


def normalise_number(text: str) -> str:
    """The account as it is written on a line.

    `Nexterpay 5`, `BBS 5` and `5` are the same account. The desk writes the
    first, their list writes the second, and the parser hands over whatever it
    found - so the prefix is dropped and what is left is the number.
    """
    cleaned = (text or "").strip()
    for prefix in ("nexterpay", "bbs", "bbd", "spex"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    return cleaned.lstrip("-:# ").strip()


async def clients_on_account(
    session: AsyncSession, *, supplier_code: str, number: str
) -> list[Client]:
    """The registered clients whose business runs through this account.

    Only the ones that resolve. A row naming a client this platform has never
    heard of is kept - the list is NexterPay's, not ours, and dropping rows we
    cannot match would quietly lose the mapping the day that client is
    registered - but it cannot narrow anything until there is something to
    narrow to.
    """
    supplier = (supplier_code or "").strip().upper()
    wanted = normalise_number(number)
    if not supplier or not wanted:
        return []

    result = await session.execute(
        select(SupplierAccount).where(
            SupplierAccount.supplier_code == supplier,
            SupplierAccount.number == wanted,
            SupplierAccount.client_id.is_not(None),
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    found = await session.execute(
        select(Client).where(Client.id.in_({row.client_id for row in rows}))
    )
    return list(found.scalars().all())


async def is_known(
    session: AsyncSession, *, supplier_code: str, number: str
) -> bool:
    """Do we hold this account at all?

    Different from "does it resolve to a client". An account we have never
    heard of may be a typo or a new account; one we hold but cannot resolve is
    a client who is not registered here yet. The first is worth not acting on;
    the second is worth not treating as a mistake.
    """
    result = await session.execute(
        select(SupplierAccount).where(
            SupplierAccount.supplier_code == (supplier_code or "").strip().upper(),
            SupplierAccount.number == normalise_number(number),
        )
    )
    return result.scalars().first() is not None


async def record(
    session: AsyncSession,
    *,
    supplier_code: str,
    number: str,
    client_name: str,
) -> SupplierAccount:
    """Add one line of the mapping, and link it to a client if we know them.

    Idempotent on the three together, because the list will be re-sent as it
    changes and reloading it must not multiply the rows.
    """
    supplier = supplier_code.strip().upper()
    cleaned_number = normalise_number(number)
    name = client_name.strip()

    existing = await session.execute(
        select(SupplierAccount).where(
            SupplierAccount.supplier_code == supplier,
            SupplierAccount.number == cleaned_number,
            SupplierAccount.client_name == name,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = SupplierAccount(
            supplier_code=supplier, number=cleaned_number, client_name=name
        )
        session.add(row)

    # Re-linked every time, so a client registered after the list was loaded
    # starts resolving without the list having to be sent again.
    match = await session.execute(select(Client).where(Client.name == name))
    client = match.scalar_one_or_none()
    row.client_id = client.id if client else None

    await session.flush()
    return row


__all__ = ["clients_on_account", "is_known", "normalise_number", "record"]
