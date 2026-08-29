from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StageName(StrEnum):
    CLASSIFY = "classify"
    EXTRACT = "extract"
    RECONCILE = "reconcile"
    CHECK_RULES = "check_rules"
    DRAFT = "draft"


class StageResult(ContractModel):
    status: Literal["completed", "skipped", "retry", "escalated"]
    artifact_hashes: tuple[str, ...]
    next_stage: StageName | None
    reason: str | None = None


class SourceKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TEXT = "text"
    MARKDOWN = "markdown"


class SourceCoordinate(ContractModel):
    kind: SourceKind
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class Citation(ContractModel):
    document_version_id: UUID
    source_chunk_id: UUID
    coordinate: SourceCoordinate


type FactScalar = str | int | float | bool


class Fact(ContractModel):
    id: UUID
    name: str
    value: FactScalar
    unit: str | None = None
    citations: tuple[Citation, ...] = Field(min_length=1)


class ArtifactKind(StrEnum):
    CLASSIFICATION = "classification"
    FACT_SET = "fact_set"
    EXCLUDED_CLAIMS = "excluded_claims"
    CONTRADICTION_SET = "contradiction_set"
    RULE_FINDINGS = "rule_findings"
    CHANGE_REGISTER = "change_register"
    EXECUTIVE_BRIEF = "executive_brief"
    ANALYSIS_SNAPSHOT = "analysis_snapshot"
    EXECUTION_EVIDENCE = "execution_evidence"


class StageArtifact(ContractModel):
    hash: str
    kind: ArtifactKind
    media_type: str
    size_bytes: int | None = Field(default=None, ge=0)


class DependencyKind(StrEnum):
    TARGET_DOCUMENT_VERSION = "target_document_version"
    SOURCE_VERSION = "source_version"
    RULE_SET_VERSION = "rule_set_version"
    POLICY_VERSION = "policy_version"
    PROPOSAL = "proposal"
    WORKFLOW_ARTIFACT = "workflow_artifact"


class DependencyRef(ContractModel):
    kind: DependencyKind
    object_id: UUID
    content_hash: str


class ReviewItem(ContractModel):
    id: UUID
    batch_id: UUID
    title: str
    before: str
    after: str
    reason: str
    dependencies: tuple[DependencyRef, ...]


class ReviewDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReviewDecision(ContractModel):
    item_id: UUID
    decision: ReviewDecisionKind
    note: str | None = None

    @model_validator(mode="after")
    def require_rejection_note(self) -> Self:
        if self.decision is ReviewDecisionKind.REJECT and not (self.note or "").strip():
            raise ValueError("note is required when rejecting a review item")
        return self


class QueueCommandState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    APPLIED = "applied"
    STALE = "stale"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class QueueCommand(ContractModel):
    id: UUID
    project_id: UUID
    idempotency_key: str
    sequence: int = Field(ge=0)
    reviewed_digest: str
    payload: dict[str, Any]
    state: QueueCommandState = QueueCommandState.PENDING
    created_at: datetime | None = None


class ProviderJobStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    CONTINUE_PROMPT = "continue_prompt"
    AWAITING_APPROVAL = "awaiting_approval"
    PREVIEW_READY = "preview_ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProviderJob(ContractModel):
    id: UUID
    provider: str
    operation: str
    status: ProviderJobStatus
    external_id: str | None = None
    error: dict[str, Any] | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    created_at: datetime | None = None


class ProblemDetails(ContractModel):
    type: str
    title: str
    status: int = Field(ge=400, le=599)
    detail: str
    instance: str | None = None
    correlation_id: str | None = None
