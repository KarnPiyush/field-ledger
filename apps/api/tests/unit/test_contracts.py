from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from fieldledger.config import Settings
from fieldledger.db import check_database
from fieldledger.domain.models import (
    ArtifactKind,
    Citation,
    DependencyKind,
    DependencyRef,
    Fact,
    ProblemDetails,
    ProviderJob,
    ProviderJobStatus,
    QueueCommand,
    QueueCommandState,
    ReviewDecision,
    ReviewDecisionKind,
    ReviewItem,
    SourceCoordinate,
    SourceKind,
    StageArtifact,
    StageName,
    StageResult,
)
from fieldledger.main import app
from fieldledger.persistence.tables import metadata
from pydantic import ValidationError
from sqlalchemy import DateTime, UniqueConstraint, Uuid


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_stage_contract_serializes_to_stable_wire_values() -> None:
    result = StageResult(
        status="completed",
        artifact_hashes=("sha256:register", "sha256:brief"),
        next_stage=StageName.CHECK_RULES,
    )

    assert tuple(stage.value for stage in StageName) == (
        "classify",
        "extract",
        "reconcile",
        "check_rules",
        "draft",
    )
    assert result.model_dump(mode="json") == {
        "status": "completed",
        "artifact_hashes": ["sha256:register", "sha256:brief"],
        "next_stage": "check_rules",
        "reason": None,
    }


def test_domain_value_objects_round_trip_as_json() -> None:
    project_id = uuid4()
    batch_id = uuid4()
    item_id = uuid4()
    coordinate = SourceCoordinate(kind=SourceKind.PDF, page=2, paragraph=4)
    document_version_id = uuid4()
    source_chunk_id = uuid4()
    citation = Citation(
        document_version_id=document_version_id,
        source_chunk_id=source_chunk_id,
        coordinate=coordinate,
    )
    dependency = DependencyRef(
        kind=DependencyKind.SOURCE_VERSION,
        object_id=uuid4(),
        content_hash="sha256:source",
    )
    fact = Fact(
        id=uuid4(),
        name="contract_value",
        value="1250000",
        unit="USD",
        citations=(citation,),
    )
    artifact = StageArtifact(
        hash="sha256:artifact",
        kind=ArtifactKind.CHANGE_REGISTER,
        media_type="application/json",
    )
    item = ReviewItem(
        id=item_id,
        batch_id=batch_id,
        title="Contract value changed",
        before="USD 1,200,000",
        after="USD 1,250,000",
        reason="Approved change order",
        dependencies=(dependency,),
    )
    command = QueueCommand(
        id=uuid4(),
        project_id=project_id,
        idempotency_key="phone-1:7",
        sequence=7,
        reviewed_digest="sha256:review-vector",
        payload={"review_item_id": str(item_id), "decision": "approve"},
    )
    provider_job = ProviderJob(
        id=uuid4(),
        provider="deterministic",
        operation="draft",
        status=ProviderJobStatus.AWAITING_APPROVAL,
    )

    payloads = [fact, artifact, dependency, item, command, provider_job]
    dumped = [value.model_dump(mode="json") for value in payloads]

    assert dumped[0]["citations"][0] == {
        "document_version_id": str(document_version_id),
        "source_chunk_id": str(source_chunk_id),
        "coordinate": {
            "kind": "pdf",
            "page": 2,
            "paragraph": 4,
            "table": None,
            "row": None,
            "column": None,
            "line_start": None,
            "line_end": None,
        },
    }
    assert dumped[1]["kind"] == "change_register"
    assert dumped[2]["kind"] == "source_version"
    assert dumped[3]["dependencies"][0]["content_hash"] == "sha256:source"
    assert dumped[4]["state"] == "pending"
    assert dumped[5]["status"] == "awaiting_approval"
    assert UUID(dumped[4]["project_id"]) == project_id


def test_fact_requires_at_least_one_resolvable_citation() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Fact(
            id=uuid4(),
            name="unsupported_claim",
            value="not publishable",
            citations=(),
        )


def test_rejection_requires_a_nonblank_note() -> None:
    with pytest.raises(ValidationError, match="note is required"):
        ReviewDecision(
            item_id=uuid4(),
            decision=ReviewDecisionKind.REJECT,
            note="  ",
        )


def test_queue_and_provider_enums_cover_recovery_states() -> None:
    assert {state.value for state in QueueCommandState} == {
        "pending",
        "sending",
        "applied",
        "stale",
        "failed",
        "reconciliation_required",
    }
    assert {status.value for status in ProviderJobStatus} == {
        "pending",
        "in_progress",
        "continue_prompt",
        "awaiting_approval",
        "preview_ready",
        "completed",
        "failed",
        "cancelled",
        "reconciliation_required",
    }


def test_problem_details_uses_rfc_9457_shape() -> None:
    problem = ProblemDetails(
        type="https://fieldledger.local/problems/database-unavailable",
        title="Database unavailable",
        status=503,
        detail="The readiness probe could not reach PostgreSQL.",
        correlation_id="health-check",
    )

    assert problem.model_dump(mode="json", exclude_none=True) == {
        "type": "https://fieldledger.local/problems/database-unavailable",
        "title": "Database unavailable",
        "status": 503,
        "detail": "The readiness probe could not reach PostgreSQL.",
        "correlation_id": "health-check",
    }


def test_deterministic_provider_is_the_no_key_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIELDLEDGER_PROVIDER", raising=False)

    assert Settings().provider == "deterministic"


def test_initial_metadata_contains_the_complete_durable_schema() -> None:
    expected_tables = {
        "projects",
        "project_memberships",
        "documents",
        "document_aliases",
        "document_versions",
        "rule_sets",
        "ingestion_outcomes",
        "source_chunks",
        "workflow_runs",
        "run_input_manifest",
        "run_invalidation_entries",
        "stage_attempts",
        "artifact_blobs",
        "stage_artifacts",
        "stage_dependencies",
        "facts",
        "citations",
        "contradictions",
        "rule_findings",
        "review_batches",
        "review_items",
        "review_dependencies",
        "review_decisions",
        "confirmation_tokens",
        "queued_commands",
        "idempotency_keys",
        "review_commands",
        "provider_sessions",
        "provider_session_events",
        "provider_jobs",
        "audit_events",
        "outbox",
    }

    assert set(metadata.tables) == expected_tables
    for table in metadata.tables.values():
        assert table.primary_key.columns.keys() == ["id"]
        assert isinstance(table.c.id.type, Uuid)
        assert isinstance(table.c.created_at.type, DateTime)
        assert table.c.created_at.type.timezone is True


def test_initial_metadata_has_claim_replay_and_outbox_guards() -> None:
    unique_constraints = {
        constraint.name
        for table in metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    index_names = {index.name for table in metadata.tables.values() for index in table.indexes}

    assert {
        "uq_document_versions_document_number",
        "uq_queued_commands_idempotency_key",
        "uq_queued_commands_client_sequence",
        "uq_review_dependencies_vector_entry",
        "uq_artifact_blobs_hash",
        "uq_stage_artifacts_attempt_blob_kind",
        "uq_confirmation_tokens_hash",
        "uq_confirmation_tokens_nonce",
        "uq_provider_sessions_project_external",
        "uq_provider_session_events_session_external",
        "uq_project_memberships_project_actor",
        "uq_review_commands_project_key",
    } <= unique_constraints
    assert {
        "ix_stage_attempts_claim",
        "ix_provider_jobs_status",
        "ix_confirmation_tokens_expiry",
        "ix_provider_sessions_project_status",
        "ix_project_memberships_actor",
        "ix_review_commands_recovery",
        "ix_outbox_unpublished",
    } <= index_names


def test_live_health_contract(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_ready_health_contract_when_database_is_available(client: TestClient) -> None:
    async def database_ready() -> bool:
        return True

    app.dependency_overrides[check_database] = database_ready

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ready"}


def test_ready_health_returns_problem_when_database_is_unavailable(
    client: TestClient,
) -> None:
    async def database_unavailable() -> bool:
        return False

    app.dependency_overrides[check_database] = database_unavailable

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://fieldledger.local/problems/database-unavailable",
        "title": "Database unavailable",
        "status": 503,
        "detail": "The readiness probe could not reach PostgreSQL.",
    }
