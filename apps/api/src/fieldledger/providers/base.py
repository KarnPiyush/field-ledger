from __future__ import annotations

from enum import StrEnum
from typing import Any, BinaryIO, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderOutcomeStatus(StrEnum):
    PROCESSING = "processing"
    REVIEW_READY = "review_ready"
    PREVIEW_READY = "preview_ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProviderDocument(ProviderModel):
    id: str
    version: str
    media_type: str = "application/octet-stream"
    content: bytes = b""


class EditRequest(ProviderModel):
    document_id: str
    prompt: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class ProviderJob(ProviderModel):
    id: str
    document_id: str | None = None
    status: ProviderOutcomeStatus
    retry_after_seconds: float | None = Field(default=None, ge=0)


class ProposedChange(ProviderModel):
    id: str
    content: dict[str, Any]


class ItemApproval(ProviderModel):
    change_id: str
    approved: bool
    note: str | None = None

    @model_validator(mode="after")
    def require_denial_note(self) -> ItemApproval:
        if not self.approved and not (self.note or "").strip():
            raise ValueError("note is required for a denied change")
        return self


class ApprovalRequest(ProviderModel):
    job_id: str
    document_id: str
    approved: bool
    changes: tuple[ItemApproval, ...] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class ProviderDocumentState(ProviderModel):
    document_id: str
    version: str
    media_type: str = "application/octet-stream"
    content: bytes = b""
    pending_changes: tuple[ProposedChange, ...] = ()
    last_idempotency_key: str | None = None
    last_outcome: ProviderOutcomeStatus | None = None
    unexpected_events: tuple[dict[str, Any], ...] = ()


class ProviderOutcome(ProviderModel):
    job_id: str
    status: ProviderOutcomeStatus
    proposed_changes: tuple[ProposedChange, ...] = ()
    document: ProviderDocument | None = None
    incremental_events: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)

    @property
    def is_loading(self) -> bool:
        return self.status is ProviderOutcomeStatus.PROCESSING


class ExportRequest(ProviderModel):
    document_id: str
    format: str = Field(min_length=1)


class ExportedFile(ProviderModel):
    name: str
    media_type: str
    content: bytes


class ProviderPayloadError(ValueError):
    pass


class ProviderMutationUnknown(RuntimeError):
    pass


class ProviderProblem(RuntimeError):
    def __init__(
        self,
        *,
        status: int,
        kind: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"provider request failed: {kind} ({status})")
        self.status = status
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


class DocumentProvider(Protocol):
    async def upload(
        self, source: BinaryIO, *, idempotency_key: str
    ) -> ProviderDocument: ...

    async def start_edit(self, request: EditRequest) -> ProviderJob: ...

    async def poll(self, job_id: str) -> ProviderOutcome: ...

    async def approve(self, request: ApprovalRequest) -> ProviderOutcome: ...

    async def read_document(self, document_id: str) -> ProviderDocumentState: ...

    async def export(self, request: ExportRequest) -> ExportedFile: ...
