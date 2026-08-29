"""add immutable ingestion records

Revision ID: 0002_ingestion
Revises: 0001
Create Date: 2026-08-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_ingestion"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    historical_versions = int(
        op.get_bind().scalar(sa.text("SELECT count(*) FROM document_versions")) or 0
    )
    if historical_versions:
        raise RuntimeError(
            "cannot add authoritative raw bytes: historical document versions exist "
            "without recoverable source bytes"
        )
    op.add_column(
        "document_versions",
        sa.Column(
            "raw_content",
            sa.LargeBinary(),
            nullable=False,
        ),
    )
    op.create_table(
        "rule_sets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("raw_content", sa.LargeBinary(), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_rule_sets_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_sets")),
        sa.UniqueConstraint("project_id", "content_hash", name="uq_rule_sets_project_hash"),
        sa.UniqueConstraint("project_id", "version_number", name="uq_rule_sets_project_number"),
    )
    op.create_table(
        "ingestion_outcomes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("problem", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_ingestion_outcomes_document_version_id_document_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_ingestion_outcomes_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_outcomes")),
    )
    op.create_index(
        "ix_ingestion_outcomes_project_created",
        "ingestion_outcomes",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_outcomes_project_created", table_name="ingestion_outcomes")
    op.drop_table("ingestion_outcomes")
    op.drop_table("rule_sets")
    op.drop_column("document_versions", "raw_content")
