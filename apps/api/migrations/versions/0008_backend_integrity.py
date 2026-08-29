"""pin provider commands and expose review evidence

Revision ID: 0008_backend_integrity
Revises: 0007_secure_review_lifecycle
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_backend_integrity"
down_revision: str | Sequence[str] | None = "0007_secure_review_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_sessions", sa.Column("local_document_version_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_provider_sessions_local_version",
        "provider_sessions",
        "document_versions",
        ["local_document_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "provider_jobs", sa.Column("idempotency_key_hash", sa.String(length=80), nullable=True)
    )
    op.add_column(
        "provider_jobs", sa.Column("pinned_document_version_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_provider_jobs_pinned_version",
        "provider_jobs",
        "document_versions",
        ["pinned_document_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_provider_jobs_project_idempotency",
        "provider_jobs",
        ["project_id", "idempotency_key_hash"],
        unique=True,
        postgresql_where=sa.text("idempotency_key_hash IS NOT NULL"),
    )
    op.add_column(
        "review_items",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "review_items",
        sa.Column("citation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_items", "citation")
    op.drop_column("review_items", "evidence")
    op.drop_index("uq_provider_jobs_project_idempotency", table_name="provider_jobs")
    op.drop_constraint(
        "fk_provider_jobs_pinned_version",
        "provider_jobs",
        type_="foreignkey",
    )
    op.drop_column("provider_jobs", "pinned_document_version_id")
    op.drop_column("provider_jobs", "idempotency_key_hash")
    op.drop_constraint(
        "fk_provider_sessions_local_version",
        "provider_sessions",
        type_="foreignkey",
    )
    op.drop_column("provider_sessions", "local_document_version_id")
