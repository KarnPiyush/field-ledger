from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def id_column() -> Column[UUID]:
    return Column(
        "id", Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def created_at_column() -> Column[datetime]:
    return Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )


projects = Table(
    "projects",
    metadata,
    id_column(),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("status", String(32), nullable=False, server_default="active"),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)

documents = Table(
    "documents",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(255), nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("source_path", Text),
    Column("fingerprint", String(80), nullable=False),
    created_at_column(),
    UniqueConstraint("project_id", "fingerprint", name="uq_documents_project_fingerprint"),
)

document_versions = Table(
    "document_versions",
    metadata,
    id_column(),
    Column(
        "document_id",
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("version_number", Integer, nullable=False),
    Column("content_hash", String(80), nullable=False),
    Column("storage_uri", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column(
        "raw_content",
        LargeBinary,
        nullable=False,
    ),
    created_at_column(),
    UniqueConstraint(
        "document_id",
        "version_number",
        name="uq_document_versions_document_number",
    ),
    UniqueConstraint(
        "document_id",
        "content_hash",
        name="uq_document_versions_document_hash",
    ),
)
Index("ix_document_versions_content_hash", document_versions.c.content_hash)

document_aliases = Table(
    "document_aliases",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "document_id",
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_path", String(255), nullable=False),
    Column("normalized_path", String(255), nullable=False),
    created_at_column(),
    Column(
        "last_seen_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "project_id", "normalized_path", name="uq_document_aliases_project_path"
    ),
)
Index("ix_document_aliases_document", document_aliases.c.document_id)

rule_sets = Table(
    "rule_sets",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("version_number", Integer, nullable=False),
    Column("version", String(200), nullable=False),
    Column("content_hash", String(80), nullable=False),
    Column("raw_content", LargeBinary, nullable=False),
    Column("rules", JSONB, nullable=False),
    created_at_column(),
    UniqueConstraint("project_id", "version_number", name="uq_rule_sets_project_number"),
    UniqueConstraint("project_id", "content_hash", name="uq_rule_sets_project_hash"),
)

ingestion_outcomes = Table(
    "ingestion_outcomes",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_name", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("content_hash", String(80)),
    Column(
        "document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
    ),
    Column("problem", JSONB),
    created_at_column(),
)
Index(
    "ix_ingestion_outcomes_project_created",
    ingestion_outcomes.c.project_id,
    ingestion_outcomes.c.created_at,
)

source_chunks = Table(
    "source_chunks",
    metadata,
    id_column(),
    Column(
        "document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("content_hash", String(80), nullable=False),
    Column("content", Text, nullable=False),
    Column("coordinate", JSONB, nullable=False),
    created_at_column(),
    UniqueConstraint(
        "document_version_id",
        "ordinal",
        name="uq_source_chunks_version_ordinal",
    ),
)
Index("ix_source_chunks_content_hash", source_chunks.c.content_hash)

workflow_runs = Table(
    "workflow_runs",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("current_stage", String(32)),
    Column("input_hash", String(80), nullable=False),
    Column(
        "supersedes_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
    ),
    Column(
        "stage_input_hashes",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("failure", JSONB),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint(
        "supersedes_run_id", name="uq_workflow_runs_supersedes_run_id"
    ),
)

project_memberships = Table(
    "project_memberships",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("actor_id", String(200), nullable=False),
    Column("role", String(32), nullable=False),
    Column("granted_by", String(200), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "project_id", "actor_id", name="uq_project_memberships_project_actor"
    ),
)
Index("ix_project_memberships_actor", project_memberships.c.actor_id)
Index("ix_workflow_runs_project_status", workflow_runs.c.project_id, workflow_runs.c.status)

run_input_manifest = Table(
    "run_input_manifest",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", String(64), nullable=False),
    Column("object_id", Uuid(as_uuid=True), nullable=False),
    Column("content_hash", String(80), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "workflow_run_id", "kind", "object_id", name="uq_run_input_manifest_entry"
    ),
)
Index(
    "ix_run_input_manifest_lookup",
    run_input_manifest.c.workflow_run_id,
    run_input_manifest.c.kind,
)

run_invalidation_entries = Table(
    "run_invalidation_entries",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "previous_document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("earliest_stage", String(32), nullable=False),
    Column("affected_block_key", String(255), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "workflow_run_id",
        "document_version_id",
        "affected_block_key",
        name="uq_run_invalidation_entry",
    ),
)
Index(
    "ix_run_invalidation_entries_run_stage",
    run_invalidation_entries.c.workflow_run_id,
    run_invalidation_entries.c.earliest_stage,
)

stage_attempts = Table(
    "stage_attempts",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("stage", String(32), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("input_hash", String(80), nullable=False),
    Column("lease_owner", String(200)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("fencing_token", BigInteger, nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("duration_ms", BigInteger),
    Column("estimated_cost", Numeric(14, 6)),
    Column("failure", JSONB),
    Column("result", JSONB),
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("invalidated_at", DateTime(timezone=True)),
    Column("invalidation_reason", Text),
    created_at_column(),
    UniqueConstraint(
        "workflow_run_id",
        "stage",
        "attempt_number",
        name="uq_stage_attempts_run_stage_number",
    ),
)
Index(
    "ix_stage_attempts_claim",
    stage_attempts.c.status,
    stage_attempts.c.lease_expires_at,
)

artifact_blobs = Table(
    "artifact_blobs",
    metadata,
    id_column(),
    Column("artifact_hash", String(80), nullable=False),
    Column("media_type", String(100), nullable=False),
    Column("storage_uri", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("content", LargeBinary, nullable=False),
    created_at_column(),
    UniqueConstraint("artifact_hash", name="uq_artifact_blobs_hash"),
)

stage_artifacts = Table(
    "stage_artifacts",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "stage_attempt_id",
        Uuid(as_uuid=True),
        ForeignKey("stage_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "artifact_blob_id",
        Uuid(as_uuid=True),
        ForeignKey("artifact_blobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("kind", String(64), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "stage_attempt_id",
        "artifact_blob_id",
        "kind",
        name="uq_stage_artifacts_attempt_blob_kind",
    ),
)

stage_dependencies = Table(
    "stage_dependencies",
    metadata,
    id_column(),
    Column(
        "stage_artifact_id",
        Uuid(as_uuid=True),
        ForeignKey("stage_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", String(64), nullable=False),
    Column("dependency_key", String(255), nullable=False),
    Column("content_hash", String(80), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "stage_artifact_id",
        "kind",
        "dependency_key",
        name="uq_stage_dependencies_artifact_entry",
    ),
)
Index(
    "ix_stage_dependencies_lookup", stage_dependencies.c.kind, stage_dependencies.c.dependency_key
)

facts = Table(
    "facts",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("fact_key", String(200), nullable=False),
    Column("value", JSONB, nullable=False),
    Column("unit", String(64)),
    Column("supported", Boolean, nullable=False, server_default=text("true")),
    created_at_column(),
    UniqueConstraint("workflow_run_id", "fact_key", name="uq_facts_run_key"),
)

citations = Table(
    "citations",
    metadata,
    id_column(),
    Column(
        "fact_id",
        Uuid(as_uuid=True),
        ForeignKey("facts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "source_chunk_id",
        Uuid(as_uuid=True),
        ForeignKey("source_chunks.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("coordinate", JSONB, nullable=False),
    Column("quoted_text", Text, nullable=False),
    created_at_column(),
    UniqueConstraint("fact_id", "source_chunk_id", name="uq_citations_fact_chunk"),
)

contradictions = Table(
    "contradictions",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("contradiction_key", String(200), nullable=False),
    Column("fact_ids", JSONB, nullable=False),
    Column("summary", Text, nullable=False),
    created_at_column(),
    UniqueConstraint(
        "workflow_run_id",
        "contradiction_key",
        name="uq_contradictions_run_key",
    ),
)

rule_findings = Table(
    "rule_findings",
    metadata,
    id_column(),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("rule_id", String(200), nullable=False),
    Column("status", String(32), nullable=False),
    Column("message", Text, nullable=False),
    Column("fact_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    created_at_column(),
    UniqueConstraint("workflow_run_id", "rule_id", name="uq_rule_findings_run_rule"),
)

review_batches = Table(
    "review_batches",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "workflow_run_id",
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "supersedes_batch_id",
        Uuid(as_uuid=True),
        ForeignKey("review_batches.id", ondelete="RESTRICT"),
    ),
    Column(
        "provider_session_id",
        Uuid(as_uuid=True),
        ForeignKey("provider_sessions.id", ondelete="RESTRICT"),
    ),
    Column(
        "provider_job_id",
        Uuid(as_uuid=True),
        ForeignKey("provider_jobs.id", ondelete="SET NULL"),
    ),
    Column("author_actor_id", String(200)),
    Column("proposal_hash", String(80)),
    Column("status", String(32), nullable=False, server_default="open"),
    Column("dependency_digest", String(80), nullable=False),
    Column("retained_decisions", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    created_at_column(),
    UniqueConstraint(
        "workflow_run_id",
        "dependency_digest",
        name="uq_review_batches_run_digest",
    ),
)

review_items = Table(
    "review_items",
    metadata,
    id_column(),
    Column(
        "review_batch_id",
        Uuid(as_uuid=True),
        ForeignKey("review_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("item_key", String(200), nullable=False),
    Column("title", String(255), nullable=False),
    Column("before_text", Text, nullable=False),
    Column("after_text", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("citation", JSONB),
    Column("status", String(32), nullable=False, server_default="pending"),
    created_at_column(),
    UniqueConstraint("review_batch_id", "item_key", name="uq_review_items_batch_key"),
)

review_dependencies = Table(
    "review_dependencies",
    metadata,
    id_column(),
    Column(
        "review_batch_id",
        Uuid(as_uuid=True),
        ForeignKey("review_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", String(64), nullable=False),
    Column("dependency_key", String(255), nullable=False),
    Column("content_hash", String(80), nullable=False),
    created_at_column(),
    UniqueConstraint(
        "review_batch_id",
        "kind",
        "dependency_key",
        name="uq_review_dependencies_vector_entry",
    ),
)

review_decisions = Table(
    "review_decisions",
    metadata,
    id_column(),
    Column(
        "review_item_id",
        Uuid(as_uuid=True),
        ForeignKey("review_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("actor_id", String(200), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("note", Text),
    Column("confirmation_token_hash", String(80), nullable=False),
    created_at_column(),
    UniqueConstraint("review_item_id", name="uq_review_decisions_item"),
)

confirmation_tokens = Table(
    "confirmation_tokens",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "review_batch_id",
        Uuid(as_uuid=True),
        ForeignKey("review_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("actor_id", String(200), nullable=False),
    Column("token_hash", String(80), nullable=False),
    Column("nonce", String(64), nullable=False),
    Column("dependency_digest", String(80), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    created_at_column(),
    UniqueConstraint("token_hash", name="uq_confirmation_tokens_hash"),
    UniqueConstraint("nonce", name="uq_confirmation_tokens_nonce"),
)
Index(
    "ix_confirmation_tokens_expiry",
    confirmation_tokens.c.expires_at,
    confirmation_tokens.c.consumed_at,
)

queued_commands = Table(
    "queued_commands",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("client_id", String(200), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("reviewed_digest", String(80), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", String(32), nullable=False, server_default="pending"),
    Column("last_error", JSONB),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("idempotency_key", name="uq_queued_commands_idempotency_key"),
    UniqueConstraint(
        "client_id",
        "sequence",
        name="uq_queued_commands_client_sequence",
    ),
)
Index(
    "ix_queued_commands_drain",
    queued_commands.c.client_id,
    queued_commands.c.state,
    queued_commands.c.sequence,
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("key", String(255), nullable=False),
    Column("request_hash", String(80), nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", JSONB, nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    created_at_column(),
    UniqueConstraint("project_id", "key", name="uq_idempotency_keys_project_key"),
)

review_commands = Table(
    "review_commands",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "review_batch_id",
        Uuid(as_uuid=True),
        ForeignKey("review_batches.id", ondelete="SET NULL"),
    ),
    Column("key", String(255), nullable=False),
    Column("request_hash", String(80), nullable=False),
    Column("confirmed_actor_id", String(200), nullable=False),
    Column("relay_actor_id", String(200), nullable=False),
    Column("confirmation_token_hash", String(80), nullable=False),
    Column("status", String(32), nullable=False, server_default="reserved"),
    Column("owner_token", String(64)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("response_status", Integer),
    Column("response_body", JSONB),
    Column("last_error", JSONB),
    Column("attempts", Integer, nullable=False, server_default="0"),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("project_id", "key", name="uq_review_commands_project_key"),
)
Index(
    "ix_review_commands_recovery",
    review_commands.c.status,
    review_commands.c.lease_expires_at,
)

provider_sessions = Table(
    "provider_sessions",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "document_id",
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
    ),
    Column("provider", String(64), nullable=False),
    Column("external_session_id", String(255), nullable=False),
    Column("external_document_id", String(255), nullable=False),
    Column(
        "local_document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
    ),
    Column("status", String(32), nullable=False, server_default="active"),
    Column("fence_token", BigInteger, nullable=False, server_default="0"),
    Column("last_document_version", String(255)),
    Column("last_event_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column(
        "last_pending_change_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    ),
    Column("quarantine_reason", Text),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "project_id", "external_session_id", name="uq_provider_sessions_project_external"
    ),
)
Index(
    "ix_provider_sessions_project_status",
    provider_sessions.c.project_id,
    provider_sessions.c.status,
)

provider_session_events = Table(
    "provider_session_events",
    metadata,
    id_column(),
    Column(
        "provider_session_id",
        Uuid(as_uuid=True),
        ForeignKey("provider_sessions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("external_event_id", String(255), nullable=False),
    Column("event_hash", String(80), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("quarantined", Boolean, nullable=False, server_default=text("false")),
    created_at_column(),
    UniqueConstraint(
        "provider_session_id",
        "external_event_id",
        name="uq_provider_session_events_session_external",
    ),
)

provider_jobs = Table(
    "provider_jobs",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "document_id",
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
    ),
    Column(
        "provider_session_id",
        Uuid(as_uuid=True),
        ForeignKey("provider_sessions.id", ondelete="SET NULL"),
    ),
    Column("fence_token", BigInteger),
    Column("idempotency_key_hash", String(80)),
    Column(
        "pinned_document_version_id",
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
    ),
    Column("provider", String(64), nullable=False),
    Column("external_id", String(255)),
    Column("operation", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("request", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("result", JSONB),
    Column("error", JSONB),
    Column("retry_after_seconds", Numeric(12, 3)),
    created_at_column(),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("terminal_at", DateTime(timezone=True)),
    UniqueConstraint("provider", "external_id", name="uq_provider_jobs_external"),
)
Index("ix_provider_jobs_status", provider_jobs.c.status, provider_jobs.c.updated_at)
Index(
    "uq_provider_jobs_project_idempotency",
    provider_jobs.c.project_id,
    provider_jobs.c.idempotency_key_hash,
    unique=True,
    postgresql_where=provider_jobs.c.idempotency_key_hash.is_not(None),
)

audit_events = Table(
    "audit_events",
    metadata,
    id_column(),
    Column(
        "project_id",
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("actor_id", String(200), nullable=False),
    Column("actor_source", String(64), nullable=False),
    Column("event_type", String(100), nullable=False),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", Uuid(as_uuid=True), nullable=False),
    Column("prior_version", String(80)),
    Column("new_version", String(80)),
    Column("command_id", Uuid(as_uuid=True)),
    Column("decision_id", Uuid(as_uuid=True)),
    Column("artifact_hashes", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    created_at_column(),
)
Index("ix_audit_events_project_created", audit_events.c.project_id, audit_events.c.created_at)

outbox = Table(
    "outbox",
    metadata,
    id_column(),
    Column("topic", String(100), nullable=False),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", Uuid(as_uuid=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("published_at", DateTime(timezone=True)),
    Column("last_error", Text),
    created_at_column(),
)
Index(
    "ix_outbox_unpublished",
    outbox.c.created_at,
    postgresql_where=outbox.c.published_at.is_(None),
)
