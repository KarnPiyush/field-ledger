from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fieldledger.domain.models import ContractModel


class DemoSessionResponse(ContractModel):
    access_token: str
    token_type: str
    actor_id: str
    source: str
    global_role: str


class ProjectResponse(ContractModel):
    id: UUID
    name: str
    description: str | None = None
    status: str


class ProjectListResponse(ContractModel):
    items: list[ProjectResponse]
    total: int
    limit: int
    offset: int


class SourceResponse(ContractModel):
    project_id: UUID
    source_name: str
    status: str
    content_hash: str | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    version_number: int | None = None
    previous_version_id: UUID | None = None
    previous_content_hash: str | None = None
    run_id: UUID | None = None
    problem: dict[str, Any] | None = None


class RunResponse(ContractModel):
    id: UUID
    project_id: UUID | None = None
    status: str
    current_stage: str | None = None


class ReviewSummaryResponse(ContractModel):
    id: UUID
    project_id: UUID
    status: str
    dependency_digest: str | None = None


class ReviewItemResponse(ContractModel):
    id: UUID
    title: str
    before: str
    after: str
    reason: str
    status: str
    evidence: dict[str, Any] = {}
    citation: dict[str, Any] | None = None


class ReviewBatchResponse(ReviewSummaryResponse):
    items: list[ReviewItemResponse]


class ReviewListResponse(ContractModel):
    items: list[ReviewSummaryResponse]
    total: int
    limit: int
    offset: int


class ConfirmationResponse(ContractModel):
    token: str
    dependency_digest: str
    expires_at: datetime


class AuditEventResponse(ContractModel):
    id: UUID | None = None
    event_type: str
    actor_id: str | None = None
    actor_source: str | None = None
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None
    prior_version: str | None = None
    new_version: str | None = None
    command_id: UUID | None = None
    decision_id: UUID | None = None
    artifact_hashes: list[str] = []
    payload: dict[str, Any] | None = None
    created_at: datetime | None = None


class AuditListResponse(ContractModel):
    items: list[AuditEventResponse]
    total: int
    limit: int
    offset: int


class ProviderJobResponse(ContractModel):
    id: UUID
    status: str
    review_batch_id: UUID | None = None
    retry_after_seconds: float | None = None
    error: dict[str, Any] | None = None


class DemoSeedResponse(ContractModel):
    project_id: UUID
    workflow_run_id: UUID
    review_batch_id: UUID
    document_id: UUID
    status: str
    replayed: bool


class ExportResponse(ContractModel):
    document_id: UUID
    status: str
    name: str
    media_type: str
    content_base64: str
