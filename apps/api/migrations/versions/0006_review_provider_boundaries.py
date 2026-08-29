"""add review confirmation and provider session boundaries

Revision ID: 0006_review_provider_boundaries
Revises: 0005_successor_invalidation
Create Date: 2026-08-23 00:00:04.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_review_provider_boundaries"
down_revision: str | Sequence[str] | None = "0005_successor_invalidation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_session_id", sa.String(length=255), nullable=False),
        sa.Column("external_document_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("fence_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_document_version", sa.String(length=255), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_provider_sessions_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_provider_sessions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_sessions")),
        sa.UniqueConstraint(
            "project_id",
            "external_session_id",
            name="uq_provider_sessions_project_external",
        ),
    )
    op.create_index(
        "ix_provider_sessions_project_status",
        "provider_sessions",
        ["project_id", "status"],
        unique=False,
    )
    op.create_table(
        "provider_session_events",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider_session_id", sa.Uuid(), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_hash", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quarantined", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["provider_session_id"],
            ["provider_sessions.id"],
            name=op.f("fk_provider_session_events_provider_session_id_provider_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_session_events")),
        sa.UniqueConstraint(
            "provider_session_id",
            "external_event_id",
            name="uq_provider_session_events_session_external",
        ),
    )
    op.add_column("review_batches", sa.Column("provider_session_id", sa.Uuid(), nullable=True))
    op.add_column("review_batches", sa.Column("provider_job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "review_batches", sa.Column("author_actor_id", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "review_batches", sa.Column("proposal_hash", sa.String(length=80), nullable=True)
    )
    op.add_column(
        "review_batches",
        sa.Column(
            "retained_decisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f("fk_review_batches_provider_session_id_provider_sessions"),
        "review_batches",
        "provider_sessions",
        ["provider_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_review_batches_provider_job_id_provider_jobs"),
        "review_batches",
        "provider_jobs",
        ["provider_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("provider_jobs", sa.Column("provider_session_id", sa.Uuid(), nullable=True))
    op.add_column("provider_jobs", sa.Column("fence_token", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_provider_jobs_provider_session_id_provider_sessions"),
        "provider_jobs",
        "provider_sessions",
        ["provider_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "confirmation_tokens",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.String(length=80), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("dependency_digest", sa.String(length=80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_confirmation_tokens_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_id"],
            ["review_batches.id"],
            name=op.f("fk_confirmation_tokens_review_batch_id_review_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confirmation_tokens")),
        sa.UniqueConstraint("nonce", name="uq_confirmation_tokens_nonce"),
        sa.UniqueConstraint("token_hash", name="uq_confirmation_tokens_hash"),
    )
    op.create_index(
        "ix_confirmation_tokens_expiry",
        "confirmation_tokens",
        ["expires_at", "consumed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_confirmation_tokens_expiry", table_name="confirmation_tokens")
    op.drop_table("confirmation_tokens")
    op.drop_constraint(
        op.f("fk_provider_jobs_provider_session_id_provider_sessions"),
        "provider_jobs",
        type_="foreignkey",
    )
    op.drop_column("provider_jobs", "fence_token")
    op.drop_column("provider_jobs", "provider_session_id")
    op.drop_constraint(
        op.f("fk_review_batches_provider_job_id_provider_jobs"),
        "review_batches",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_review_batches_provider_session_id_provider_sessions"),
        "review_batches",
        type_="foreignkey",
    )
    op.drop_column("review_batches", "retained_decisions")
    op.drop_column("review_batches", "proposal_hash")
    op.drop_column("review_batches", "author_actor_id")
    op.drop_column("review_batches", "provider_job_id")
    op.drop_column("review_batches", "provider_session_id")
    op.drop_table("provider_session_events")
    op.drop_index("ix_provider_sessions_project_status", table_name="provider_sessions")
    op.drop_table("provider_sessions")
