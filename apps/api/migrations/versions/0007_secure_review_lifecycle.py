"""secure review authority and provider lifecycle

Revision ID: 0007_secure_review_lifecycle
Revises: 0006_review_provider_boundaries
Create Date: 2026-08-23 00:00:05.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_secure_review_lifecycle"
down_revision: str | Sequence[str] | None = "0006_review_provider_boundaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    project_count = int(
        op.get_bind().scalar(sa.text("SELECT count(*) FROM projects")) or 0
    )
    if project_count:
        raise RuntimeError(
            "project membership migration requires an explicit project membership mapping"
        )
    op.create_table(
        "project_memberships",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("granted_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "actor_id", name="uq_project_memberships_project_actor"
        ),
    )
    op.create_index(
        "ix_project_memberships_actor", "project_memberships", ["actor_id"], unique=False
    )
    op.create_table(
        "review_commands",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=80), nullable=False),
        sa.Column("confirmed_actor_id", sa.String(length=200), nullable=False),
        sa.Column("relay_actor_id", sa.String(length=200), nullable=False),
        sa.Column("confirmation_token_hash", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="reserved", nullable=False),
        sa.Column("owner_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["review_batch_id"], ["review_batches.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "key", name="uq_review_commands_project_key"),
    )
    op.create_index(
        "ix_review_commands_recovery",
        "review_commands",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.add_column(
        "provider_sessions",
        sa.Column(
            "last_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "provider_sessions",
        sa.Column(
            "last_pending_change_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_sessions", "last_pending_change_ids")
    op.drop_column("provider_sessions", "last_event_ids")
    op.drop_index("ix_review_commands_recovery", table_name="review_commands")
    op.drop_table("review_commands")
    op.drop_index("ix_project_memberships_actor", table_name="project_memberships")
    op.drop_table("project_memberships")
