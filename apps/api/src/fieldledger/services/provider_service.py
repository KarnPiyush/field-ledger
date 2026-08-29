from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.persistence.tables import (
    audit_events,
    document_versions,
    documents,
    outbox,
    provider_jobs,
    provider_session_events,
    provider_sessions,
    review_batches,
    review_dependencies,
    review_items,
    run_input_manifest,
    workflow_runs,
)
from fieldledger.providers.base import (
    DocumentProvider,
    EditRequest,
    ProviderMutationUnknown,
    ProviderOutcomeStatus,
)
from fieldledger.security import Actor, ProjectAuthorizer, ProjectRole
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.mutation_fence import acquire_project_mutation_fence


class ProviderJobNotFoundError(LookupError):
    status = 404
    code = "provider-job-not-found"


class ProviderDocumentNotFoundError(LookupError):
    status = 404
    code = "provider-document-not-found"


class ProviderLifecycleConflictError(RuntimeError):
    status = 409
    code = "provider-lifecycle-conflict"


class ProviderService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: DocumentProvider,
        *,
        authorization: ProjectAuthorizer,
        dependencies: DependencyService,
    ) -> None:
        self.sessions = sessions
        self.provider = provider
        self.authorization = authorization
        self.dependencies = dependencies
        self.provider_name = type(provider).__name__

    async def start_edit(
        self,
        *,
        document_id: UUID,
        workflow_run_id: UUID,
        prompt: str,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        timestamp = datetime.now(UTC)
        idempotency_hash = f"sha256:{sha256(idempotency_key.encode()).hexdigest()}"
        prompt_hash = f"sha256:{sha256(prompt.encode()).hexdigest()}"
        reserved_id: UUID
        pinned: Any
        provider_session: Any | None

        async with self.sessions.begin() as session:
            project_id_value = cast(
                UUID | None,
                await session.scalar(
                    select(documents.c.project_id).where(documents.c.id == document_id)
                ),
            )
            if project_id_value is None:
                raise ProviderDocumentNotFoundError(f"document not found: {document_id}")
            project_id = project_id_value
            await acquire_project_mutation_fence(session, project_id)
            await self.authorization.require(
                actor,
                project_id,
                roles={ProjectRole.REVIEWER, ProjectRole.OPERATOR, ProjectRole.AGENT},
                session=session,
            )
            run = (
                await session.execute(
                    select(workflow_runs.c.project_id, workflow_runs.c.status).where(
                        workflow_runs.c.id == workflow_run_id
                    )
                )
            ).one_or_none()
            if run is None or run.project_id != project_id:
                raise ProviderLifecycleConflictError("workflow run does not belong to document")
            if run.status != "review_ready":
                raise ProviderLifecycleConflictError("workflow run is not review ready")
            pinned = (
                await session.execute(
                    select(
                        document_versions.c.id,
                        document_versions.c.content_hash,
                        document_versions.c.raw_content,
                    )
                    .join(
                        run_input_manifest,
                        run_input_manifest.c.object_id == document_versions.c.id,
                    )
                    .where(
                        document_versions.c.document_id == document_id,
                        run_input_manifest.c.workflow_run_id == workflow_run_id,
                        run_input_manifest.c.kind == "source_version",
                    )
                )
            ).one_or_none()
            if pinned is None:
                raise ProviderLifecycleConflictError(
                    "target document is not pinned in the reviewed run manifest"
                )
            existing_job = (
                await session.execute(
                    select(provider_jobs)
                    .where(
                        provider_jobs.c.project_id == project_id,
                        provider_jobs.c.idempotency_key_hash == idempotency_hash,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if existing_job is not None:
                request = existing_job["request"] or {}
                if (
                    existing_job["document_id"] != document_id
                    or request.get("workflow_run_id") != str(workflow_run_id)
                    or request.get("prompt_hash") != prompt_hash
                ):
                    raise ProviderLifecycleConflictError(
                        "idempotency key was used with a different edit request"
                    )
                if existing_job["external_id"] is not None or existing_job["status"] not in {
                    "reconciliation_required",
                }:
                    return self._job_response(existing_job)
                reserved_id = cast(UUID, existing_job["id"])
                pinned = (
                    await session.execute(
                        select(
                            document_versions.c.id,
                            document_versions.c.content_hash,
                            document_versions.c.raw_content,
                        ).where(
                            document_versions.c.id
                            == existing_job["pinned_document_version_id"]
                        )
                    )
                ).one()
                provider_session = (
                    await session.execute(
                        select(provider_sessions).where(
                            provider_sessions.c.id == existing_job["provider_session_id"]
                        )
                    )
                ).mappings().one_or_none()
            else:
                reserved_id = uuid4()
                provider_session = (
                    await session.execute(
                        select(provider_sessions).where(
                            provider_sessions.c.project_id == project_id,
                            provider_sessions.c.document_id == document_id,
                            provider_sessions.c.local_document_version_id == pinned.id,
                            provider_sessions.c.status == "active",
                        )
                    )
                ).mappings().one_or_none()
                await session.execute(
                    insert(provider_jobs).values(
                        id=reserved_id,
                        project_id=project_id,
                        document_id=document_id,
                        provider_session_id=(
                            provider_session["id"] if provider_session is not None else None
                        ),
                        pinned_document_version_id=pinned.id,
                        idempotency_key_hash=idempotency_hash,
                        provider=self.provider_name,
                        operation="edit",
                        status="starting",
                        request={
                            "workflow_run_id": str(workflow_run_id),
                            "document_version_id": str(pinned.id),
                            "content_hash": pinned.content_hash,
                            "prompt_hash": prompt_hash,
                        },
                    )
                )

        if provider_session is None:
            uploaded = await self.provider.upload(
                BytesIO(pinned.raw_content),
                idempotency_key=f"provider-upload:{reserved_id}",
            )
            async with self.sessions.begin() as session:
                await acquire_project_mutation_fence(session, project_id)
                provider_session_id = cast(
                    UUID,
                    (
                        await session.execute(
                            postgres_insert(provider_sessions)
                            .values(
                                project_id=project_id,
                                document_id=document_id,
                                provider=self.provider_name,
                                external_session_id=uploaded.id,
                                external_document_id=uploaded.id,
                                local_document_version_id=pinned.id,
                                status="active",
                                last_document_version=uploaded.version,
                                last_event_ids=[],
                                last_pending_change_ids=[],
                            )
                            .on_conflict_do_update(
                                index_elements=[
                                    provider_sessions.c.project_id,
                                    provider_sessions.c.external_session_id,
                                ],
                                set_={"updated_at": timestamp},
                            )
                            .returning(provider_sessions.c.id)
                        )
                    ).scalar_one(),
                )
                await session.execute(
                    update(provider_jobs)
                    .where(provider_jobs.c.id == reserved_id)
                    .values(provider_session_id=provider_session_id, updated_at=timestamp)
                )
            external_document_id = uploaded.id
        else:
            provider_session_id = cast(UUID, provider_session["id"])
            external_document_id = str(provider_session["external_document_id"])

        try:
            job = await self.provider.start_edit(
                EditRequest(
                    document_id=external_document_id,
                    prompt=prompt,
                    idempotency_key=idempotency_key,
                )
            )
        except ProviderMutationUnknown:
            async with self.sessions.begin() as session:
                await session.execute(
                    update(provider_jobs)
                    .where(provider_jobs.c.id == reserved_id)
                    .values(
                        status="reconciliation_required",
                        error={"kind": "provider_mutation_unknown"},
                        updated_at=timestamp,
                    )
                )
            raise

        async with self.sessions.begin() as session:
            await acquire_project_mutation_fence(session, project_id)
            await session.execute(
                update(provider_jobs)
                .where(provider_jobs.c.id == reserved_id)
                .values(
                    provider_session_id=provider_session_id,
                    external_id=job.id,
                    status=job.status.value,
                    retry_after_seconds=job.retry_after_seconds,
                    error=None,
                    updated_at=timestamp,
                )
            )
            await self._audit(
                session,
                project_id=project_id,
                actor=actor,
                event_type="provider.edit_started",
                aggregate_id=reserved_id,
                status=job.status.value,
                timestamp=timestamp,
            )
        return {
            "id": reserved_id,
            "status": (
                "processing"
                if job.status is ProviderOutcomeStatus.PROCESSING
                else job.status.value
            ),
            "retry_after_seconds": job.retry_after_seconds,
        }

    async def lookup_edit(
        self,
        *,
        document_id: UUID,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        idempotency_hash = f"sha256:{sha256(idempotency_key.encode()).hexdigest()}"
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(provider_jobs).where(
                        provider_jobs.c.document_id == document_id,
                        provider_jobs.c.idempotency_key_hash == idempotency_hash,
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                raise ProviderJobNotFoundError("provider edit reservation not found")
            await self.authorization.require(
                actor,
                row["project_id"],
                roles={ProjectRole.REVIEWER, ProjectRole.OPERATOR, ProjectRole.AGENT},
                session=session,
            )
            return self._job_response(row)

    async def poll(self, job_id: UUID, actor: Actor) -> dict[str, Any]:
        timestamp = datetime.now(UTC)
        review_batch_id: UUID | None = None
        async with self.sessions.begin() as session:
            locked = (
                await session.execute(
                    select(provider_jobs).where(provider_jobs.c.id == job_id).with_for_update()
                )
            ).mappings().one_or_none()
            if locked is None:
                raise ProviderJobNotFoundError(f"provider job not found: {job_id}")
            await acquire_project_mutation_fence(session, locked["project_id"])
            await self.authorization.require(
                actor,
                locked["project_id"],
                roles={ProjectRole.REVIEWER, ProjectRole.OPERATOR, ProjectRole.AGENT},
                session=session,
            )
            if locked["status"] in {
                "review_ready",
                "preview_ready",
                "completed",
                "failed",
                "cancelled",
                "reconciliation_required",
            } and locked["result"]:
                return self._job_response(locked)
            if not locked["external_id"]:
                return self._job_response(locked)
            provider_session = (
                await session.execute(
                    select(provider_sessions)
                    .where(provider_sessions.c.id == locked["provider_session_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if provider_session is None:
                raise ProviderLifecycleConflictError("provider session is missing")

            outcome = await self.provider.poll(locked["external_id"])
            state = await self.provider.read_document(
                provider_session["external_document_id"]
            )
            expected_pending = {change.id for change in outcome.proposed_changes}
            expected_events = set(provider_session["last_event_ids"] or ()) | {
                self._event_id(event) for event in outcome.incremental_events
            }
            observed_events = {
                self._event_id(event) for event in state.unexpected_events
            }
            out_of_band = (
                state.version != provider_session["last_document_version"]
                or {change.id for change in state.pending_changes} != expected_pending
                or bool(observed_events - expected_events)
            )
            if out_of_band:
                for event in state.unexpected_events:
                    event_id = self._event_id(event)
                    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
                    await session.execute(
                        postgres_insert(provider_session_events)
                        .values(
                            provider_session_id=provider_session["id"],
                            external_event_id=event_id,
                            event_hash=f"sha256:{sha256(canonical.encode()).hexdigest()}",
                            payload=event,
                            quarantined=True,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                provider_session_events.c.provider_session_id,
                                provider_session_events.c.external_event_id,
                            ]
                        )
                    )
                await session.execute(
                    update(provider_sessions)
                    .where(provider_sessions.c.id == provider_session["id"])
                    .values(
                        status="quarantined",
                        quarantine_reason="provider state changed outside the expected job",
                        updated_at=timestamp,
                    )
                )
                await session.execute(
                    update(provider_jobs)
                    .where(provider_jobs.c.id == job_id)
                    .values(
                        status="reconciliation_required",
                        result={"review_batch_id": None},
                        error={"kind": "provider_state_mismatch"},
                        updated_at=timestamp,
                    )
                )
                await self._audit(
                    session,
                    project_id=locked["project_id"],
                    actor=actor,
                    event_type="provider.reconciliation_required",
                    aggregate_id=job_id,
                    status="reconciliation_required",
                    timestamp=timestamp,
                )
                return {
                    "id": job_id,
                    "status": "reconciliation_required",
                    "review_batch_id": None,
                    "retry_after_seconds": outcome.retry_after_seconds,
                    "error": {"kind": "provider_state_mismatch"},
                }

            existing_batch = await session.scalar(
                select(review_batches.c.id).where(review_batches.c.provider_job_id == job_id)
            )
            if outcome.status in {
                ProviderOutcomeStatus.REVIEW_READY,
                ProviderOutcomeStatus.PREVIEW_READY,
            } and outcome.proposed_changes:
                review_batch_id = cast(UUID | None, existing_batch)
                if review_batch_id is None:
                    review_batch_id = uuid4()
                    proposal_json = json.dumps(
                        [change.model_dump(mode="json") for change in outcome.proposed_changes],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    proposal_hash = f"sha256:{sha256(proposal_json.encode()).hexdigest()}"
                    vector = await self.dependencies.snapshot_for_proposal(
                        project_id=locked["project_id"],
                        target_document_id=locked["document_id"],
                        target_document_version_id=locked["pinned_document_version_id"],
                        workflow_run_id=UUID(str(locked["request"]["workflow_run_id"])),
                        batch_id=review_batch_id,
                        proposal_hash=proposal_hash,
                        session=session,
                    )
                    workflow_run_id = UUID(str(locked["request"]["workflow_run_id"]))
                    await session.execute(
                        insert(review_batches).values(
                            id=review_batch_id,
                            project_id=locked["project_id"],
                            workflow_run_id=workflow_run_id,
                            provider_session_id=provider_session["id"],
                            provider_job_id=job_id,
                            author_actor_id="provider",
                            proposal_hash=proposal_hash,
                            status="open",
                            dependency_digest=vector.digest,
                        )
                    )
                    await session.execute(
                        insert(review_items),
                        [
                            {
                                "review_batch_id": review_batch_id,
                                "item_key": change.id,
                                "title": str(change.content.get("title") or change.id),
                                "before_text": str(change.content.get("before") or ""),
                                "after_text": str(change.content.get("after") or ""),
                                "reason": str(change.content.get("reason") or "provider proposal"),
                                "evidence": {},
                                "citation": None,
                            }
                            for change in outcome.proposed_changes
                        ],
                    )
                    await session.execute(
                        insert(review_dependencies),
                        [
                            {
                                "review_batch_id": review_batch_id,
                                "kind": entry.kind.value,
                                "dependency_key": entry.key,
                                "content_hash": entry.content_hash,
                            }
                            for entry in vector.entries
                        ],
                    )
                    await self._audit(
                        session,
                        project_id=locked["project_id"],
                        actor=actor,
                        event_type="provider.review_ready",
                        aggregate_id=review_batch_id,
                        status=outcome.status.value,
                        timestamp=timestamp,
                    )
            await session.execute(
                update(provider_sessions)
                .where(provider_sessions.c.id == provider_session["id"])
                .values(
                    last_document_version=state.version,
                    last_pending_change_ids=sorted(expected_pending),
                    last_event_ids=sorted(expected_events),
                    updated_at=timestamp,
                )
            )
            await session.execute(
                update(provider_jobs)
                .where(provider_jobs.c.id == job_id)
                .values(
                    status=outcome.status.value,
                    result={
                        "proposed_change_ids": [change.id for change in outcome.proposed_changes],
                        "review_batch_id": str(review_batch_id) if review_batch_id else None,
                    },
                    retry_after_seconds=outcome.retry_after_seconds,
                    error=outcome.error,
                    terminal_at=(
                        timestamp
                        if outcome.status
                        in {
                            ProviderOutcomeStatus.COMPLETED,
                            ProviderOutcomeStatus.FAILED,
                            ProviderOutcomeStatus.CANCELLED,
                        }
                        else None
                    ),
                    updated_at=timestamp,
                )
            )
        return {
            "id": job_id,
            "status": outcome.status.value,
            "review_batch_id": review_batch_id,
            "retry_after_seconds": outcome.retry_after_seconds,
            "error": outcome.error,
        }

    async def get_job(self, job_id: UUID, actor: Actor) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                await session.execute(select(provider_jobs).where(provider_jobs.c.id == job_id))
            ).mappings().one_or_none()
            if row is None:
                raise ProviderJobNotFoundError(f"provider job not found: {job_id}")
            await self.authorization.require(actor, row["project_id"], session=session)
            return self._job_response(row)

    @staticmethod
    def _job_response(row: Any) -> dict[str, Any]:
        status = row["status"]
        if status in {"starting", "pending", "in_progress", "continue_prompt"}:
            status = "processing"
        return {
            "id": row["id"],
            "status": status,
            "review_batch_id": (
                UUID(row["result"]["review_batch_id"])
                if row["result"] and row["result"].get("review_batch_id")
                else None
            ),
            "retry_after_seconds": (
                float(row["retry_after_seconds"])
                if row["retry_after_seconds"] is not None
                else None
            ),
            "error": row["error"],
        }

    @staticmethod
    def _event_id(event: dict[str, Any]) -> str:
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        return str(event.get("id") or f"sha256:{sha256(canonical.encode()).hexdigest()}")

    @staticmethod
    async def _audit(
        session: AsyncSession,
        *,
        project_id: UUID,
        actor: Actor,
        event_type: str,
        aggregate_id: UUID,
        status: str,
        timestamp: datetime,
    ) -> None:
        await session.execute(
            insert(audit_events).values(
                project_id=project_id,
                actor_id=actor.actor_id,
                actor_source=actor.source.value,
                event_type=event_type,
                aggregate_type="provider_job",
                aggregate_id=aggregate_id,
                payload={"status": status},
                created_at=timestamp,
            )
        )
        await session.execute(
            insert(outbox).values(
                topic=event_type,
                aggregate_type="provider_job",
                aggregate_id=aggregate_id,
                payload={"status": status},
                created_at=timestamp,
            )
        )
