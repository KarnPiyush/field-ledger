from __future__ import annotations

from hashlib import sha256
from typing import BinaryIO

from fieldledger.providers.base import (
    ApprovalRequest,
    EditRequest,
    ExportedFile,
    ExportRequest,
    ProposedChange,
    ProviderDocument,
    ProviderDocumentState,
    ProviderJob,
    ProviderMutationUnknown,
    ProviderOutcome,
    ProviderOutcomeStatus,
)


class DeterministicDocumentProvider:
    """No-key provider with stable, inspectable document and job state."""

    def __init__(self, *, approve_failure: str | None = None) -> None:
        self.documents: dict[str, ProviderDocumentState] = {}
        self.jobs: dict[str, ProviderOutcome] = {}
        self.uploads: dict[str, str] = {}
        self.rounds: dict[str, int] = {}
        self.approvals: dict[str, ProviderOutcome] = {}
        self.approve_failure = approve_failure

    async def upload(
        self, source: BinaryIO, *, idempotency_key: str
    ) -> ProviderDocument:
        if idempotency_key in self.uploads:
            state = self.documents[self.uploads[idempotency_key]]
            return ProviderDocument(
                id=state.document_id,
                version=state.version,
                media_type=state.media_type,
                content=state.content,
            )
        content = source.read()
        digest = sha256(content).hexdigest()
        document_id = f"det-doc-{sha256(idempotency_key.encode()).hexdigest()[:16]}"
        document = ProviderDocumentState(
            document_id=document_id,
            version=f"sha256:{digest}",
            content=content,
        )
        self.documents[document_id] = document
        self.uploads[idempotency_key] = document_id
        return ProviderDocument(
            id=document.document_id,
            version=document.version,
            media_type=document.media_type,
            content=document.content,
        )

    async def start_edit(self, request: EditRequest) -> ProviderJob:
        if request.document_id not in self.documents:
            raise LookupError(f"document not found: {request.document_id}")
        job_digest = sha256(
            f"{request.document_id}:{request.idempotency_key}".encode()
        ).hexdigest()
        job_id = f"det-job-{job_digest[:16]}"
        self.rounds.setdefault(job_id, 1)
        if request.prompt == "Seed Change order 014 cited review":
            specifications = (
                (
                    "Contract value",
                    "USD 1,000,000",
                    "USD 1,000,000",
                    "Confirm the executed contract baseline.",
                ),
                (
                    "Feeder allowance",
                    "USD 42,500",
                    "USD 47,250",
                    "Reconcile Change order 014 with the signed change value.",
                ),
                (
                    "Completion date",
                    "2027-02-28",
                    "2027-03-15",
                    "Review the revised completion date against policy.",
                ),
                (
                    "Installed quantity",
                    "400 m",
                    "420 m",
                    "Publish the field quantity from Site Report 31.",
                ),
                (
                    "Invoice amount",
                    "USD 0",
                    "USD 122,500",
                    "Tie Invoice 1042 to the cited project record.",
                ),
            )
            proposed = tuple(
                ProposedChange(
                    id=f"{job_id}-change-{index}",
                    content={
                        "title": title,
                        "before": before,
                        "after": after,
                        "reason": reason,
                    },
                )
                for index, (title, before, after, reason) in enumerate(
                    specifications, start=1
                )
            )
        else:
            change = self._change(job_id, request.prompt)
            companion = ProposedChange(
                id=f"{job_id}-support-{self.rounds[job_id]}",
                content={
                    "before": "deterministic support before",
                    "after": f"supporting evidence for {request.prompt}",
                    "reason": f"deterministic support round {self.rounds[job_id]}",
                },
            )
            proposed = (change, companion)
        self.jobs[job_id] = ProviderOutcome(
            job_id=job_id,
            status=ProviderOutcomeStatus.REVIEW_READY,
            proposed_changes=proposed,
        )
        self.documents[request.document_id] = self.documents[request.document_id].model_copy(
            update={"pending_changes": proposed}
        )
        return ProviderJob(
            id=job_id,
            document_id=request.document_id,
            status=ProviderOutcomeStatus.PROCESSING,
        )

    async def poll(self, job_id: str) -> ProviderOutcome:
        try:
            return self.jobs[job_id]
        except KeyError as error:
            raise LookupError(f"job not found: {job_id}") from error

    async def approve(self, request: ApprovalRequest) -> ProviderOutcome:
        if request.idempotency_key in self.approvals:
            return self.approvals[request.idempotency_key]
        expected_change_ids = {
            change.id for change in self.jobs[request.job_id].proposed_changes
        }
        unknown = {
            change.change_id for change in request.changes
        } - expected_change_ids
        if unknown:
            raise ValueError(f"unknown provider change: {sorted(unknown)[0]}")
        if self.approve_failure == "before":
            raise ProviderMutationUnknown("deterministic mutation response unavailable")
        rejected = tuple(item for item in request.changes if not item.approved)
        if rejected:
            self.rounds[request.job_id] = self.rounds.get(request.job_id, 1) + 1
            change = self._change(
                request.job_id,
                "; ".join(item.note or "revise" for item in rejected),
            )
            outcome = ProviderOutcome(
                job_id=request.job_id,
                status=ProviderOutcomeStatus.REVIEW_READY,
                proposed_changes=(change,),
            )
            state = self.documents[request.document_id].model_copy(
                update={
                    "pending_changes": (change,),
                    "last_idempotency_key": request.idempotency_key,
                    "last_outcome": ProviderOutcomeStatus.REVIEW_READY,
                }
            )
        else:
            before = self.documents[request.document_id]
            content = b"\n".join(
                [before.content, *(item.change_id.encode() for item in request.changes)]
            )
            state = before.model_copy(
                update={
                    "content": content,
                    "version": f"sha256:{sha256(content).hexdigest()}",
                    "pending_changes": (),
                    "last_idempotency_key": request.idempotency_key,
                    "last_outcome": ProviderOutcomeStatus.COMPLETED,
                }
            )
            outcome = ProviderOutcome(
                job_id=request.job_id,
                status=ProviderOutcomeStatus.COMPLETED,
                document=ProviderDocument(
                    id=state.document_id,
                    version=state.version,
                    media_type=state.media_type,
                    content=state.content,
                ),
            )
        self.documents[request.document_id] = state
        self.jobs[request.job_id] = outcome
        self.approvals[request.idempotency_key] = outcome
        if self.approve_failure == "after":
            raise ProviderMutationUnknown("deterministic mutation response lost")
        return outcome

    async def read_document(self, document_id: str) -> ProviderDocumentState:
        try:
            return self.documents[document_id]
        except KeyError as error:
            raise LookupError(f"document not found: {document_id}") from error

    async def export(self, request: ExportRequest) -> ExportedFile:
        state = await self.read_document(request.document_id)
        media_types = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
        }
        return ExportedFile(
            name=f"{request.document_id}.{request.format}",
            media_type=media_types.get(request.format, "application/octet-stream"),
            content=state.content,
        )

    def _change(self, job_id: str, instruction: str) -> ProposedChange:
        round_number = self.rounds[job_id]
        return ProposedChange(
            id=f"{job_id}-change-{round_number}",
            content={
                "before": "deterministic before",
                "after": instruction,
                "reason": f"deterministic review round {round_number}",
            },
        )
