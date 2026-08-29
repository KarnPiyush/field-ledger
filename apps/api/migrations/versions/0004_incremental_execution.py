"""retain serialized incremental run lineage

Revision ID: 0004_incremental_execution
Revises: 0003_run_inputs
Create Date: 2026-08-23 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_incremental_execution"
down_revision: str | Sequence[str] | None = "0003_run_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["supersedes_run_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["supersedes_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
