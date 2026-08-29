from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.persistence import tables
from fieldledger.providers.base import EditRequest, ProviderMutationUnknown
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.security import (
    Actor,
    ActorSource,
    AuthorizationError,
    GlobalRole,
    ProjectAuthorizer,
    ProjectRole,
)
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.ingestion_service import IngestionService
from fieldledger.services.project_service import RunService, SourceService
from fieldledger.services.provider_service import ProviderService
from fieldledger.services.workflow_service import WorkflowService
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


class FaultInjectingProvider(DeterministicDocumentProvider):
    def __init__(self) -> None:
        super().__init__()
        self.lose_first_start_response = False
        self.start_requests: list[EditRequest] = []
        self.start_entered: asyncio.Event | None = None
        self.start_release: asyncio.Event | None = None

    async def start_edit(self, request: EditRequest):  # type: ignore[no-untyped-def]
        self.start_requests.append(request)
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        job = await super().start_edit(request)
        if self.lose_first_start_response and len(self.start_requests) == 1:
            raise ProviderMutationUnknown("edit was accepted but the response was lost")
        return job


@dataclass(frozen=True)
class ProviderContext:
    engine: AsyncEngine
    sessions: async_sessionmaker
    provider: FaultInjectingProvider
    service: ProviderService
    project_id: UUID
    document_id: UUID
    document_version_id: UUID
    run_id: UUID
    artifact_id: UUID
    reviewer: Actor
    viewer: Actor


@pytest.fixture
async def provider_context() -> AsyncIterator[ProviderContext]:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id, document_id, document_version_id, run_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    attempt_id, artifact_blob_id, artifact_id = uuid4(), uuid4(), uuid4()
    reviewer = Actor(
        actor_id="provider-reviewer",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    viewer = Actor(
        actor_id="provider-viewer",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    async with engine.begin() as connection:
        await connection.execute(
            insert(tables.projects).values(id=project_id, name="Provider security")
        )
        await connection.execute(
            insert(tables.project_memberships),
            [
                {
                    "project_id": project_id,
                    "actor_id": reviewer.actor_id,
                    "role": ProjectRole.REVIEWER.value,
                    "granted_by": "test",
                },
                {
                    "project_id": project_id,
                    "actor_id": viewer.actor_id,
                    "role": ProjectRole.VIEWER.value,
                    "granted_by": "test",
                },
            ],
        )
        await connection.execute(
            insert(tables.documents).values(
                id=document_id,
                project_id=project_id,
                name="target.txt",
                media_type="text/plain",
                source_path="target.txt",
                fingerprint=f"sha256:{uuid4().hex}",
            )
        )
        await connection.execute(
            insert(tables.document_versions).values(
                id=document_version_id,
                document_id=document_id,
                version_number=1,
                content_hash="sha256:pinned-v1",
                storage_uri="postgresql://provider-security/v1",
                byte_size=9,
                raw_content=b"target-v1",
            )
        )
        await connection.execute(
            insert(tables.workflow_runs).values(
                id=run_id,
                project_id=project_id,
                status="review_ready",
                input_hash="sha256:provider-run",
            )
        )
        await connection.execute(
            insert(tables.run_input_manifest).values(
                workflow_run_id=run_id,
                kind="source_version",
                object_id=document_version_id,
                content_hash="sha256:pinned-v1",
            )
        )
        await connection.execute(
            insert(tables.stage_attempts).values(
                id=attempt_id,
                workflow_run_id=run_id,
                stage="draft",
                attempt_number=1,
                status="succeeded",
                input_hash="sha256:provider-run",
            )
        )
        await connection.execute(
            insert(tables.artifact_blobs).values(
                id=artifact_blob_id,
                artifact_hash="sha256:provider-artifact",
                media_type="application/json",
                storage_uri="postgresql://artifacts/provider-artifact",
                byte_size=2,
                content=b"{}",
            )
        )
        await connection.execute(
            insert(tables.stage_artifacts).values(
                id=artifact_id,
                workflow_run_id=run_id,
                stage_attempt_id=attempt_id,
                artifact_blob_id=artifact_blob_id,
                kind="change_register",
            )
        )

    provider = FaultInjectingProvider()
    authorization = ProjectAuthorizer(sessions)
    service = ProviderService(
        sessions,
        provider,
        authorization=authorization,
        dependencies=DependencyService(sessions, policy_content_hash="sha256:policy-v1"),
    )
    try:
        yield ProviderContext(
            engine,
            sessions,
            provider,
            service,
            project_id,
            document_id,
            document_version_id,
            run_id,
            artifact_id,
            reviewer,
            viewer,
        )
    finally:
        async with engine.begin() as connection:
            aggregate_ids = list(
                await connection.scalars(
                    select(tables.audit_events.c.aggregate_id).where(
                        tables.audit_events.c.project_id == project_id
                    )
                )
            )
            if aggregate_ids:
                await connection.execute(
                    delete(tables.outbox).where(tables.outbox.c.aggregate_id.in_(aggregate_ids))
                )
            await connection.execute(
                delete(tables.audit_events).where(tables.audit_events.c.project_id == project_id)
            )
            await connection.execute(
                delete(tables.projects).where(tables.projects.c.id == project_id)
            )
            await connection.execute(
                delete(tables.artifact_blobs).where(tables.artifact_blobs.c.id == artifact_blob_id)
            )
        await engine.dispose()


async def test_start_edit_reserves_one_pinned_job_and_propagates_provider_key(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    first = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply cited updates",
        idempotency_key="provider-start-1",
        actor=ctx.reviewer,
    )
    replay = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply cited updates",
        idempotency_key="provider-start-1",
        actor=ctx.reviewer,
    )

    assert replay["id"] == first["id"]
    assert len(ctx.provider.start_requests) == 1
    assert ctx.provider.start_requests[0].idempotency_key == "provider-start-1"
    async with ctx.sessions() as session:
        jobs = (
            await session.execute(
                select(tables.provider_jobs).where(
                    tables.provider_jobs.c.project_id == ctx.project_id
                )
            )
        ).mappings().all()
    assert len(jobs) == 1
    assert jobs[0]["request"]["document_version_id"] == str(ctx.document_version_id)


async def test_start_edit_keeps_pinned_version_when_new_source_version_commits_mid_call(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    ctx.provider.start_entered = asyncio.Event()
    ctx.provider.start_release = asyncio.Event()
    starting = asyncio.create_task(
        ctx.service.start_edit(
            document_id=ctx.document_id,
            workflow_run_id=ctx.run_id,
            prompt="apply cited updates",
            idempotency_key="provider-pin-race-1",
            actor=ctx.reviewer,
        )
    )
    await ctx.provider.start_entered.wait()
    async with ctx.sessions.begin() as session:
        await session.execute(
            insert(tables.document_versions).values(
                document_id=ctx.document_id,
                version_number=2,
                content_hash="sha256:pinned-v2",
                storage_uri="postgresql://provider-security/v2",
                byte_size=9,
                raw_content=b"target-v2",
            )
        )
    ctx.provider.start_release.set()
    started = await starting
    ready = await ctx.service.poll(UUID(str(started["id"])), ctx.reviewer)

    async with ctx.sessions() as session:
        target_hash = await session.scalar(
            select(tables.review_dependencies.c.content_hash).where(
                tables.review_dependencies.c.review_batch_id == ready["review_batch_id"],
                tables.review_dependencies.c.kind == "target_document_version",
            )
        )
    assert target_hash == "sha256:pinned-v1"


async def test_start_edit_uses_reviewed_manifest_version_when_newer_version_already_exists(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    async with ctx.sessions.begin() as session:
        await session.execute(
            insert(tables.document_versions).values(
                document_id=ctx.document_id,
                version_number=2,
                content_hash="sha256:unreviewed-v2",
                storage_uri="postgresql://provider-security/unreviewed-v2",
                byte_size=13,
                raw_content=b"unreviewed-v2",
            )
        )

    started = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply only reviewed bytes",
        idempotency_key="provider-reviewed-version-1",
        actor=ctx.reviewer,
    )
    ready = await ctx.service.poll(UUID(str(started["id"])), ctx.reviewer)

    assert next(iter(ctx.provider.documents.values())).content == b"target-v1"
    async with ctx.sessions() as session:
        target_hash = await session.scalar(
            select(tables.review_dependencies.c.content_hash).where(
                tables.review_dependencies.c.review_batch_id == ready["review_batch_id"],
                tables.review_dependencies.c.kind == "target_document_version",
            )
        )
    assert target_hash == "sha256:pinned-v1"


async def test_ambiguous_start_is_durably_reserved_and_same_key_replay_recovers(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    ctx.provider.lose_first_start_response = True
    with pytest.raises(ProviderMutationUnknown):
        await ctx.service.start_edit(
            document_id=ctx.document_id,
            workflow_run_id=ctx.run_id,
            prompt="apply cited updates",
            idempotency_key="provider-timeout-1",
            actor=ctx.reviewer,
        )

    async with ctx.sessions() as session:
        reserved = (
            await session.execute(
                select(tables.provider_jobs).where(
                    tables.provider_jobs.c.project_id == ctx.project_id
                )
            )
        ).mappings().one()
    assert reserved["status"] == "reconciliation_required"

    replay = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply cited updates",
        idempotency_key="provider-timeout-1",
        actor=ctx.reviewer,
    )
    assert replay["id"] == reserved["id"]
    assert [item.idempotency_key for item in ctx.provider.start_requests] == [
        "provider-timeout-1",
        "provider-timeout-1",
    ]


async def test_edit_lookup_reads_ambiguous_reservation_without_provider_call(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    ctx.provider.lose_first_start_response = True
    with pytest.raises(ProviderMutationUnknown):
        await ctx.service.start_edit(
            document_id=ctx.document_id,
            workflow_run_id=ctx.run_id,
            prompt="apply cited updates",
            idempotency_key="provider-lookup-1",
            actor=ctx.reviewer,
        )
    calls_before = len(ctx.provider.start_requests)
    assert hasattr(ctx.service, "lookup_edit")

    found = await ctx.service.lookup_edit(
        document_id=ctx.document_id,
        idempotency_key="provider-lookup-1",
        actor=ctx.reviewer,
    )

    assert found["status"] == "reconciliation_required"
    assert len(ctx.provider.start_requests) == calls_before


async def test_poll_quarantines_out_of_band_state_and_creates_no_review(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    started = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply cited updates",
        idempotency_key="provider-oob-1",
        actor=ctx.reviewer,
    )
    async with ctx.sessions() as session:
        provider_session = (
            await session.execute(
                select(tables.provider_sessions).where(
                    tables.provider_sessions.c.project_id == ctx.project_id
                )
            )
        ).mappings().one()
    external_document_id = provider_session["external_document_id"]
    state = ctx.provider.documents[external_document_id]
    ctx.provider.documents[external_document_id] = state.model_copy(
        update={"unexpected_events": ({"id": "manual-edit-1", "type": "insert"},)}
    )

    result = await ctx.service.poll(UUID(str(started["id"])), ctx.reviewer)

    assert result["status"] == "reconciliation_required"
    assert result["review_batch_id"] is None
    async with ctx.sessions() as session:
        status = await session.scalar(
            select(tables.provider_sessions.c.status).where(
                tables.provider_sessions.c.id == provider_session["id"]
            )
        )
        review_count = await session.scalar(
            select(tables.review_batches.c.id).where(
                tables.review_batches.c.project_id == ctx.project_id
            )
        )
    assert status == "quarantined"
    assert review_count is None


async def test_provider_cannot_forge_authoritative_review_evidence(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    started = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="provider evidence boundary",
        idempotency_key="provider-forged-evidence-1",
        actor=ctx.reviewer,
    )
    async with ctx.sessions() as session:
        job = (
            await session.execute(
                select(tables.provider_jobs).where(
                    tables.provider_jobs.c.id == UUID(str(started["id"]))
                )
            )
        ).mappings().one()
        provider_session = (
            await session.execute(
                select(tables.provider_sessions).where(
                    tables.provider_sessions.c.id == job["provider_session_id"]
                )
            )
        ).mappings().one()
    outcome = ctx.provider.jobs[job["external_id"]]
    forged = outcome.proposed_changes[0].model_copy(
        update={
            "content": {
                **outcome.proposed_changes[0].content,
                "evidence": {"fact_key": "forged", "value": "approve me"},
                "citation": {
                    "document_version_id": str(ctx.document_version_id),
                    "source_chunk_id": str(uuid4()),
                    "quoted_text": "fabricated provider quote",
                },
            }
        }
    )
    changes = (forged, *outcome.proposed_changes[1:])
    ctx.provider.jobs[job["external_id"]] = outcome.model_copy(
        update={"proposed_changes": changes}
    )
    state = ctx.provider.documents[provider_session["external_document_id"]]
    ctx.provider.documents[provider_session["external_document_id"]] = state.model_copy(
        update={"pending_changes": changes}
    )

    ready = await ctx.service.poll(UUID(str(started["id"])), ctx.reviewer)

    async with ctx.sessions() as session:
        item = (
            await session.execute(
                select(tables.review_items)
                .where(tables.review_items.c.review_batch_id == ready["review_batch_id"])
                .where(tables.review_items.c.item_key == forged.id)
            )
        ).mappings().one()
    assert item["evidence"] == {}
    assert item["citation"] is None


async def test_review_dependencies_include_immutable_workflow_artifact(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    started = await ctx.service.start_edit(
        document_id=ctx.document_id,
        workflow_run_id=ctx.run_id,
        prompt="apply cited updates",
        idempotency_key="provider-provenance-1",
        actor=ctx.reviewer,
    )
    ready = await ctx.service.poll(UUID(str(started["id"])), ctx.reviewer)
    async with ctx.sessions() as session:
        dependencies = (
            await session.execute(
                select(tables.review_dependencies).where(
                    tables.review_dependencies.c.review_batch_id
                    == ready["review_batch_id"]
                )
            )
        ).mappings().all()
    assert any(
        row["kind"] == "workflow_artifact"
        and row["dependency_key"] == str(ctx.artifact_id)
        and row["content_hash"] == "sha256:provider-artifact"
        for row in dependencies
    )


async def test_viewer_cannot_ingest_start_run_or_start_provider_edit(
    provider_context: ProviderContext,
) -> None:
    ctx = provider_context
    authorization = ProjectAuthorizer(ctx.sessions)
    workflow = WorkflowService(ctx.sessions)
    source_service = SourceService(
        IngestionService(ctx.sessions, workflow=workflow), authorization
    )
    run_service = RunService(ctx.sessions, workflow, authorization)

    with pytest.raises(AuthorizationError):
        await source_service.add_source(
            ctx.project_id,
            name="viewer.txt",
            media_type="text/plain",
            content=b"Installed quantity: 1 m",
            actor=ctx.viewer,
        )
    with pytest.raises(AuthorizationError):
        await run_service.start_run(
            ctx.project_id, actor=ctx.viewer, idempotency_key="viewer-run"
        )
    with pytest.raises(AuthorizationError):
        await ctx.service.start_edit(
            document_id=ctx.document_id,
            workflow_run_id=ctx.run_id,
            prompt="viewer edit",
            idempotency_key="viewer-edit",
            actor=ctx.viewer,
        )
