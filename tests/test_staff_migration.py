"""The migration that lets a person work two desks.

Separate from the other tests because it does not test the code - it tests the
upgrade, with rows in the table, against a real SQLite file.

The reason is specific. Everyone registered on the live server has their
department and role in two columns that this migration drops. If the backfill
is wrong they arrive on the other side belonging to nothing, and the first
anyone knows about it is a member of staff being refused their own work on a
Monday morning. That is worth a test that actually runs the migration rather
than one that trusts it.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

BEFORE = "4c9b17ad3e62"  # connected tickets - the last revision with the old shape
AFTER = "8f4a2be05c19"  # staff can span departments

PEOPLE = [
    (5001, "Sarah Hill", "operator", "support"),
    (5002, "James Okoro", "senior_operator", "support"),
    (5003, "Priya Nair", "manager", "finance"),
    (5004, "Tom Reid", "administrator", "compliance"),
    (5005, "Ana Silva", "operator", "business"),
]


def _alembic(db: Path, *args: str) -> None:
    result = subprocess.run(
        ["python", "-m", "alembic", *args],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
            "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        },
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic {' '.join(args)} failed:\n{result.stderr}")


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    """A database at the old revision, with people in it, then upgraded."""
    db = tmp_path / "staff.db"
    _alembic(db, "upgrade", BEFORE)

    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO staff (telegram_user_id, display_name, role, department, "
            "is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, '2026-08-01 09:00:00', '2026-08-01 09:00:00')",
            PEOPLE,
        )

    _alembic(db, "upgrade", AFTER)
    return db


def test_everyone_keeps_the_department_and_role_they_had(migrated: Path) -> None:
    """Checked person by person rather than by counting rows.

    A count would pass if five rows arrived with the wrong roles on them,
    which is the failure that would actually happen - the INSERT ... SELECT
    naming the columns in the wrong order.
    """
    with sqlite3.connect(migrated) as conn:
        rows = conn.execute(
            "SELECT s.telegram_user_id, sd.role, sd.department "
            "FROM staff s JOIN staff_departments sd ON sd.staff_id = s.id"
        ).fetchall()

    carried = {user_id: (role, dept) for user_id, role, dept in rows}
    expected = {user_id: (role, dept) for user_id, _, role, dept in PEOPLE}

    assert carried == expected, "someone came out the other side changed"


def test_nobody_is_left_belonging_to_nothing(migrated: Path) -> None:
    """A person with no memberships is refused every action, so this is
    the difference between the platform working on Monday and not."""
    with sqlite3.connect(migrated) as conn:
        orphaned = conn.execute(
            "SELECT display_name FROM staff WHERE id NOT IN "
            "(SELECT staff_id FROM staff_departments)"
        ).fetchall()

    assert orphaned == [], f"these people work nowhere now: {orphaned}"


def test_the_old_columns_are_gone(migrated: Path) -> None:
    """Left behind, they become a second and quietly wrong answer to
    'what department is this person in'."""
    with sqlite3.connect(migrated) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(staff)")}

    assert "role" not in columns
    assert "department" not in columns
    assert {"telegram_user_id", "display_name", "is_active"} <= columns


def test_a_second_desk_cannot_be_added_twice(migrated: Path) -> None:
    """The unique constraint, checked against the built table rather than
    the model - a constraint declared in Python and missing from the
    migration would pass every other test in this suite."""
    with sqlite3.connect(migrated) as conn:
        staff_id = conn.execute(
            "SELECT id FROM staff WHERE telegram_user_id = 5001"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO staff_departments (staff_id, department, role, created_at, "
            "updated_at) VALUES (?, 'compliance', 'operator', '2026-09-01', '2026-09-01')",
            (staff_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO staff_departments (staff_id, department, role, created_at, "
                "updated_at) VALUES (?, 'compliance', 'manager', '2026-09-01', '2026-09-01')",
                (staff_id,),
            )


def test_it_can_be_reversed(migrated: Path) -> None:
    """Going back has to leave a working database, even though it cannot
    keep a second desk. Whoever runs a downgrade at 2am is already having a
    bad night and should not also find an empty column."""
    _alembic(migrated, "downgrade", BEFORE)

    with sqlite3.connect(migrated) as conn:
        rows = conn.execute(
            "SELECT telegram_user_id, role, department FROM staff"
        ).fetchall()

    restored = {user_id: (role, dept) for user_id, role, dept in rows}
    expected = {user_id: (role, dept) for user_id, _, role, dept in PEOPLE}
    assert restored == expected
