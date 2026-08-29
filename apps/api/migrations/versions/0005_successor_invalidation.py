"""keep invalidation on successor lineage

Revision ID: 0005_successor_invalidation
Revises: 0004_incremental_execution
Create Date: 2026-08-23 00:00:03.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_successor_invalidation"
down_revision: str | Sequence[str] | None = "0004_incremental_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_workflow_runs_supersedes_run_id",
        "workflow_runs",
        ["supersedes_run_id"],
    )
    op.create_table(
        "run_invalidation_entries",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("previous_document_version_id", sa.Uuid(), nullable=False),
        sa.Column("earliest_stage", sa.String(length=32), nullable=False),
        sa.Column("affected_block_key", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f(
                "fk_run_invalidation_entries_document_version_id_document_versions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["previous_document_version_id"],
            ["document_versions.id"],
            name=op.f(
                "fk_run_invalidation_entries_previous_document_version_id_document_versions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f(
                "fk_run_invalidation_entries_workflow_run_id_workflow_runs"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_invalidation_entries")),
        sa.UniqueConstraint(
            "workflow_run_id",
            "document_version_id",
            "affected_block_key",
            name="uq_run_invalidation_entry",
        ),
    )
    op.create_index(
        "ix_run_invalidation_entries_run_stage",
        "run_invalidation_entries",
        ["workflow_run_id", "earliest_stage"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_invalidation_entries_run_stage",
        table_name="run_invalidation_entries",
    )
    op.drop_table("run_invalidation_entries")
    op.drop_constraint(
        "uq_workflow_runs_supersedes_run_id",
        "workflow_runs",
        type_="unique",
    )
