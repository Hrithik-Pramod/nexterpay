"""staff can span departments

NexterPay confirmed during testing that they have people who genuinely work
two desks. Until now a person had one department and one role, so registering
someone for Compliance silently removed them from Support - the workaround was
to make them an administrator, which grants far more than "also works here".

Seniority becomes a fact about a person in a department rather than about the
person, because someone can reasonably be a Manager on their own desk and an
Operator on the one they help out with.

The part to be careful about is the backfill. Every existing person has to
arrive on the other side with exactly the department and role they had, or
they lose access to their own work the moment this deploys. The backfill runs
before the old columns are dropped, and `test_staff_migration.py` runs this
migration against seeded rows and checks each person individually.

Revision ID: 8f4a2be05c19
Revises: 4c9b17ad3e62
Create Date: 2026-09-01 09:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '8f4a2be05c19'
down_revision = '4c9b17ad3e62'
branch_labels = None
depends_on = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


DEPARTMENTS = ("support", "finance", "development", "business", "compliance")
ROLES = ("operator", "senior_operator", "manager", "administrator")


def _enum(values: tuple[str, ...], name: str):
    """Reuse the existing named type on Postgres rather than recreating it.

    `department` and `staff_role` already exist - the staff table has used
    both since the first migration. Without create_type=False, Alembic emits
    a second CREATE TYPE and the migration fails on the real database while
    passing happily on SQLite, which has no named types at all.
    """
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade() -> None:
    op.create_table(
        "staff_departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        sa.Column("department", _enum(DEPARTMENTS, "department"), nullable=False),
        sa.Column("role", _enum(ROLES, "staff_role"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["staff_id"], ["staff.id"], name="fk_staff_departments_staff_id_staff"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_departments"),
        sa.UniqueConstraint("staff_id", "department", name="uq_staff_department"),
    )
    op.create_index(
        "ix_staff_departments_department", "staff_departments", ["department"]
    )

    # Everyone currently registered, carried across exactly as they are. This
    # runs before the columns are dropped, so there is no window in which the
    # information exists nowhere.
    op.execute(
        """
        INSERT INTO staff_departments
            (staff_id, department, role, created_at, updated_at)
        SELECT id, department, role, created_at, updated_at FROM staff
        """
    )

    with op.batch_alter_table("staff", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_column("role")
        batch.drop_column("department")


def downgrade() -> None:
    # Coming back means choosing one desk per person, because the old shape
    # cannot hold two. The lowest department alphabetically is arbitrary but
    # deterministic; anyone who had gained a second desk would need it
    # restored by hand, which is the honest cost of going backwards.
    with op.batch_alter_table("staff", naming_convention=NAMING_CONVENTION) as batch:
        batch.add_column(
            sa.Column(
                "role", _enum(ROLES, "staff_role"), nullable=False, server_default="operator"
            )
        )
        batch.add_column(
            sa.Column(
                "department", _enum(DEPARTMENTS, "department"),
                nullable=False, server_default="support",
            )
        )

    op.execute(
        """
        UPDATE staff SET
            department = (
                SELECT department FROM staff_departments
                WHERE staff_departments.staff_id = staff.id
                ORDER BY department LIMIT 1
            ),
            role = (
                SELECT role FROM staff_departments
                WHERE staff_departments.staff_id = staff.id
                ORDER BY department LIMIT 1
            )
        WHERE EXISTS (
            SELECT 1 FROM staff_departments WHERE staff_departments.staff_id = staff.id
        )
        """
    )

    op.drop_index("ix_staff_departments_department", table_name="staff_departments")
    op.drop_table("staff_departments")
