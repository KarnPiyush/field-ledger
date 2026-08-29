"""pin run inputs and document aliases

Revision ID: 0003_run_inputs
Revises: 0002_ingestion
Create Date: 2026-08-23 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_run_inputs"
down_revision: str | Sequence[str] | None = "0002_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs", sa.Column("supersedes_run_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["supersedes_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "document_aliases",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=False),
        sa.Column("normalized_path", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_aliases_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_document_aliases_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_aliases")),
        sa.UniqueConstraint(
            "project_id",
            "normalized_path",
            name="uq_document_aliases_project_path",
        ),
    )
    op.create_index(
        "ix_document_aliases_document",
        "document_aliases",
        ["document_id"],
        unique=False,
    )
    op.create_table(
        "run_input_manifest",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_run_input_manifest_workflow_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_input_manifest")),
        sa.UniqueConstraint(
            "workflow_run_id",
            "kind",
            "object_id",
            name="uq_run_input_manifest_entry",
        ),
    )
    op.create_index(
        "ix_run_input_manifest_lookup",
        "run_input_manifest",
        ["workflow_run_id", "kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_input_manifest_lookup", table_name="run_input_manifest")
    op.drop_table("run_input_manifest")
    op.drop_index("ix_document_aliases_document", table_name="document_aliases")
    op.drop_table("document_aliases")
    op.drop_constraint(
        op.f("fk_workflow_runs_supersedes_run_id_workflow_runs"),
        "workflow_runs",
        type_="foreignkey",
    )
    op.drop_column("workflow_runs", "supersedes_run_id")
