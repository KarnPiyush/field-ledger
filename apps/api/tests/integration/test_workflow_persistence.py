import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.domain.models import ArtifactKind, DependencyKind, StageName, StageResult
from fieldledger.domain.workflow import AttemptStatus, InvalidTransitionError
from fieldledger.persistence.repositories import (
    ArtifactConflictError,
    ClaimedStage,
    FenceViolationError,
    PreparedArtifact,
    PreparedDependency,
)
from fieldledger.persistence.tables import (
    artifact_blobs,
    audit_events,
    outbox,
    projects,
    stage_artifacts,
    stage_attempts,
    stage_dependencies,
    workflow_runs,
)
from fieldledger.services.workflow_service import WorkflowService
from fieldledger.worker import StageExecution, run_worker_iteration
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class WorkflowTestContext:
    engine: AsyncEngine
    sessions: async_sessionmaker
    service: WorkflowService
    project_id: UUID


@dataclass
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
async def workflow_context() -> AsyncIterator[WorkflowTestContext]:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        server_version = int(await connection.scalar(text("SHOW server_version_num")) or 0)
        assert 170000 <= server_version < 180000, "integration tests require PostgreSQL 17"
        project_id = uuid4()
        await connection.execute(
            insert(projects).values(id=project_id, name=f"Workflow test {project_id}")
        )
    # Keep connection establishment outside the two-second deadlock assertions below.
    async with engine.connect() as first_connection, engine.connect() as second_connection:
        await asyncio.gather(
            first_connection.execute(text("SELECT 1")),
            second_connection.execute(text("SELECT 1")),
        )

    service = WorkflowService(sessions, lease_duration=timedelta(seconds=30))
    try:
        yield WorkflowTestContext(engine, sessions, service, project_id)
    finally:
        async with engine.begin() as connection:
            run_ids = set(
                (
                    await connection.execute(
                        select(workflow_runs.c.id).where(
                            workflow_runs.c.project_id == project_id
                        )
                    )
                ).scalars()
            )
            run_ids.update(
                (
                    await connection.execute(
                        select(audit_events.c.aggregate_id).where(
                            audit_events.c.project_id == project_id,
                            audit_events.c.aggregate_type == "workflow_run",
                        )
                    )
                ).scalars()
            )
            if run_ids:
                await connection.execute(
                    delete(outbox).where(outbox.c.aggregate_id.in_(run_ids))
                )
            await connection.execute(
                delete(audit_events).where(audit_events.c.project_id == project_id)
            )
            await connection.execute(delete(projects).where(projects.c.id == project_id))
            await connection.execute(
                delete(artifact_blobs).where(
                    artifact_blobs.c.id.not_in(select(stage_artifacts.c.artifact_blob_id))
                )
            )
        await engine.dispose()


def artifact(content: bytes = b'{"classification":"contract"}') -> PreparedArtifact:
    artifact_hash = f"sha256:{sha256(content).hexdigest()}"
    return PreparedArtifact(
        hash=artifact_hash,
        kind=ArtifactKind.CLASSIFICATION,
        media_type="application/json",
        content=content,
    )


def completed_result(stage: StageName, *artifacts: PreparedArtifact) -> StageResult:
    order = list(StageName)
    index = order.index(stage)
    next_stage = order[index + 1] if index + 1 < len(order) else None
    return StageResult(
        status="completed",
        artifact_hashes=tuple(item.hash for item in artifacts),
        next_stage=next_stage,
    )


async def create_run(context: WorkflowTestContext, input_hash: str = "sha256:input") -> UUID:
    return await context.service.create_run(
        project_id=context.project_id,
        input_hash=input_hash,
        now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )


async def scalar_count(context: WorkflowTestContext, table: object) -> int:
    if table is stage_attempts:
        source = stage_attempts.join(
            workflow_runs, stage_attempts.c.workflow_run_id == workflow_runs.c.id
        )
        statement = select(func.count()).select_from(source)
    elif table is stage_artifacts:
        source = stage_artifacts.join(
            workflow_runs, stage_artifacts.c.workflow_run_id == workflow_runs.c.id
        )
        statement = select(func.count()).select_from(source)
    elif table is stage_dependencies:
        source = stage_dependencies.join(stage_artifacts).join(workflow_runs)
        statement = select(func.count()).select_from(source)
    elif table is artifact_blobs:
        source = artifact_blobs.join(stage_artifacts).join(workflow_runs)
        statement = select(func.count(artifact_blobs.c.id.distinct())).select_from(source)
    elif table is audit_events:
        statement = (
            select(func.count())
            .select_from(audit_events)
            .where(audit_events.c.project_id == context.project_id)
        )
    elif table is outbox:
        source = outbox.join(workflow_runs, outbox.c.aggregate_id == workflow_runs.c.id)
        statement = select(func.count()).select_from(source)
    else:
        raise AssertionError(f"Unsupported project-scoped table: {table}")
    if table is not audit_events:
        statement = statement.where(workflow_runs.c.project_id == context.project_id)
    async with context.sessions() as session:
        return int(await session.scalar(statement) or 0)


async def complete_run(
    context: WorkflowTestContext,
    run_id: UUID,
    *,
    classify_artifact: PreparedArtifact | None = None,
    classify_dependency: PreparedDependency | None = None,
) -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    for stage in StageName:
        claim = await context.service.claim_stage(worker_id="finisher", now=now)
        assert claim is not None and claim.run_id == run_id and claim.stage is stage
        artifacts = (
            (classify_artifact,)
            if stage is StageName.CLASSIFY and classify_artifact
            else ()
        )
        dependencies = (
            (classify_dependency,)
            if stage is StageName.CLASSIFY and classify_dependency
            else ()
        )
        await context.service.commit_stage(
            claim,
            completed_result(stage, *artifacts),
            artifacts=artifacts,
            dependencies=dependencies,
            now=now,
        )


async def test_concurrent_workers_claim_one_attempt_once(
    workflow_context: WorkflowTestContext,
) -> None:
    await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    first, second = await asyncio.gather(
        workflow_context.service.claim_stage(worker_id="worker-a", now=now),
        workflow_context.service.claim_stage(worker_id="worker-b", now=now),
    )

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].stage is StageName.CLASSIFY
    assert await scalar_count(workflow_context, stage_attempts) == 1


async def test_different_runs_can_be_claimed_concurrently(
    workflow_context: WorkflowTestContext,
) -> None:
    run_a = await create_run(workflow_context, "sha256:a")
    run_b = await create_run(workflow_context, "sha256:b")
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    first, second = await asyncio.gather(
        workflow_context.service.claim_stage(worker_id="worker-a", now=now),
        workflow_context.service.claim_stage(worker_id="worker-b", now=now),
    )

    assert first is not None
    assert second is not None
    assert {first.run_id, second.run_id} == {run_a, run_b}


async def test_expired_lease_reclaims_with_a_new_fence_and_heartbeat_enforces_it(
    workflow_context: WorkflowTestContext,
) -> None:
    await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    stale = await workflow_context.service.claim_stage(worker_id="worker-a", now=now)
    assert stale is not None

    reclaimed = await workflow_context.service.claim_stage(
        worker_id="worker-b", now=now + timedelta(seconds=31)
    )

    assert reclaimed is not None
    assert reclaimed.attempt_id == stale.attempt_id
    assert reclaimed.fencing_token == stale.fencing_token + 1
    heartbeat_at = now + timedelta(seconds=32)
    heartbeat = await workflow_context.service.heartbeat_stage(reclaimed, now=heartbeat_at)
    assert heartbeat.lease_expires_at == heartbeat_at + timedelta(seconds=30)
    with pytest.raises(FenceViolationError):
        await workflow_context.service.heartbeat_stage(stale, now=heartbeat_at)
    with pytest.raises(FenceViolationError):
        await workflow_context.service.commit_stage(stale, completed_result(stale.stage))


async def test_commit_is_atomic_idempotent_and_persists_artifact_dependency_audit_outbox(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    claim = await workflow_context.service.claim_stage(
        worker_id="worker-a", now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    assert claim is not None
    output = artifact()
    dependency = PreparedDependency(
        artifact_hash=output.hash,
        kind=DependencyKind.SOURCE_VERSION,
        dependency_key="document-version:1",
        content_hash="sha256:source-v1",
    )

    first = await workflow_context.service.commit_stage(
        claim,
        completed_result(claim.stage, output),
        artifacts=(output,),
        dependencies=(dependency,),
        duration_ms=125,
        estimated_cost=Decimal("0.001250"),
    )
    second = await workflow_context.service.commit_stage(
        claim,
        completed_result(claim.stage, output),
        artifacts=(output,),
        dependencies=(dependency,),
        duration_ms=125,
        estimated_cost=Decimal("0.001250"),
    )

    assert first.attempt_id == second.attempt_id == claim.attempt_id
    assert second.replayed is True
    async with workflow_context.sessions() as session:
        attempt_row = (
            await session.execute(
                select(stage_attempts).where(stage_attempts.c.id == claim.attempt_id)
            )
        ).mappings().one()
        run_row = (
            await session.execute(select(workflow_runs).where(workflow_runs.c.id == run_id))
        ).mappings().one()
    assert attempt_row["status"] == AttemptStatus.COMPLETED.value
    assert attempt_row["duration_ms"] == 125
    assert attempt_row["estimated_cost"] == Decimal("0.001250")
    assert run_row["current_stage"] == StageName.EXTRACT.value
    assert await scalar_count(workflow_context, artifact_blobs) == 1
    assert await scalar_count(workflow_context, stage_artifacts) == 1
    assert await scalar_count(workflow_context, stage_dependencies) == 1
    assert await scalar_count(workflow_context, audit_events) == 1
    assert await scalar_count(workflow_context, outbox) == 1


async def test_conflicting_artifact_hash_and_content_rolls_back_commit(
    workflow_context: WorkflowTestContext,
) -> None:
    stored = artifact(b"first")
    first_run = await create_run(workflow_context, "sha256:first-run")
    await complete_run(workflow_context, first_run, classify_artifact=stored)

    await create_run(workflow_context, "sha256:second-run")
    claim = await workflow_context.service.claim_stage(
        worker_id="worker-a", now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    assert claim is not None
    counts_before = {
        "blobs": await scalar_count(workflow_context, artifact_blobs),
        "links": await scalar_count(workflow_context, stage_artifacts),
        "dependencies": await scalar_count(workflow_context, stage_dependencies),
        "audit": await scalar_count(workflow_context, audit_events),
        "outbox": await scalar_count(workflow_context, outbox),
    }
    conflicting = PreparedArtifact(
        hash=stored.hash,
        kind=stored.kind,
        media_type=stored.media_type,
        content=b"second",
    )
    with pytest.raises(ArtifactConflictError):
        await workflow_context.service.commit_stage(
            claim,
            completed_result(claim.stage, conflicting),
            artifacts=(conflicting,),
        )

    async with workflow_context.sessions() as session:
        status = await session.scalar(
            select(stage_attempts.c.status).where(stage_attempts.c.id == claim.attempt_id)
        )
    assert status == AttemptStatus.RUNNING.value
    assert {
        "blobs": await scalar_count(workflow_context, artifact_blobs),
        "links": await scalar_count(workflow_context, stage_artifacts),
        "dependencies": await scalar_count(workflow_context, stage_dependencies),
        "audit": await scalar_count(workflow_context, audit_events),
        "outbox": await scalar_count(workflow_context, outbox),
    } == counts_before


async def test_same_blob_has_distinct_attempt_provenance_and_survives_one_run_deletion(
    workflow_context: WorkflowTestContext,
) -> None:
    output = artifact(b"shared immutable content")
    run_a = await create_run(workflow_context, "sha256:run-a")
    await complete_run(
        workflow_context,
        run_a,
        classify_artifact=output,
        classify_dependency=PreparedDependency(
            artifact_hash=output.hash,
            kind=DependencyKind.SOURCE_VERSION,
            dependency_key="document:a",
            content_hash="sha256:source-a",
        ),
    )
    run_b = await create_run(workflow_context, "sha256:run-b")
    await complete_run(
        workflow_context,
        run_b,
        classify_artifact=output,
        classify_dependency=PreparedDependency(
            artifact_hash=output.hash,
            kind=DependencyKind.SOURCE_VERSION,
            dependency_key="document:b",
            content_hash="sha256:source-b",
        ),
    )

    async with workflow_context.sessions() as session:
        links = (
            await session.execute(
                select(stage_artifacts)
                .join(workflow_runs)
                .where(workflow_runs.c.project_id == workflow_context.project_id)
                .order_by(stage_artifacts.c.workflow_run_id)
            )
        ).mappings().all()
        dependency_rows = (
            await session.execute(
                select(stage_dependencies.c.stage_artifact_id, stage_dependencies.c.content_hash)
                .select_from(stage_dependencies.join(stage_artifacts).join(workflow_runs))
                .where(workflow_runs.c.project_id == workflow_context.project_id)
            )
        ).all()
    assert await scalar_count(workflow_context, artifact_blobs) == 1
    assert len(links) == 2
    assert {link["workflow_run_id"] for link in links} == {run_a, run_b}
    assert {row.content_hash for row in dependency_rows} == {
        "sha256:source-a",
        "sha256:source-b",
    }

    async with workflow_context.sessions.begin() as session:
        await session.execute(delete(workflow_runs).where(workflow_runs.c.id == run_a))

    async with workflow_context.sessions() as session:
        remaining_links = (
            await session.execute(
                select(stage_artifacts)
                .join(workflow_runs)
                .where(workflow_runs.c.project_id == workflow_context.project_id)
            )
        ).mappings().all()
        remaining_dependencies = (
            await session.execute(
                select(stage_dependencies.c.content_hash)
                .select_from(stage_dependencies.join(stage_artifacts).join(workflow_runs))
                .where(workflow_runs.c.project_id == workflow_context.project_id)
            )
        ).scalars().all()
    assert await scalar_count(workflow_context, artifact_blobs) == 1
    assert len(remaining_links) == 1
    assert remaining_links[0]["workflow_run_id"] == run_b
    assert remaining_dependencies == ["sha256:source-b"]


async def test_invalid_transition_changes_no_durable_state(
    workflow_context: WorkflowTestContext,
) -> None:
    await create_run(workflow_context)
    claim = await workflow_context.service.claim_stage(
        worker_id="worker-a", now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    assert claim is not None

    with pytest.raises(InvalidTransitionError):
        await workflow_context.service.commit_stage(
            claim,
            StageResult(
                status="completed",
                artifact_hashes=(),
                next_stage=StageName.DRAFT,
            ),
        )

    async with workflow_context.sessions() as session:
        status = await session.scalar(
            select(stage_attempts.c.status).where(stage_attempts.c.id == claim.attempt_id)
        )
    assert status == AttemptStatus.RUNNING.value
    assert await scalar_count(workflow_context, audit_events) == 0
    assert await scalar_count(workflow_context, outbox) == 0


@pytest.mark.parametrize("status", ["retry", "skipped", "escalated"])
async def test_blank_terminal_reason_changes_no_durable_state(
    workflow_context: WorkflowTestContext, status: str
) -> None:
    await create_run(workflow_context)
    claim = await workflow_context.service.claim_stage(
        worker_id="worker-a", now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    assert claim is not None
    next_stage = claim.stage if status == "retry" else StageName.EXTRACT
    if status == "escalated":
        next_stage = None

    with pytest.raises(InvalidTransitionError, match="nonblank reason"):
        await workflow_context.service.commit_stage(
            claim,
            StageResult(
                status=status,
                artifact_hashes=(),
                next_stage=next_stage,
                reason="   ",
            ),
        )

    async with workflow_context.sessions() as session:
        status_value = await session.scalar(
            select(stage_attempts.c.status).where(stage_attempts.c.id == claim.attempt_id)
        )
    assert status_value == AttemptStatus.RUNNING.value
    assert await scalar_count(workflow_context, audit_events) == 0
    assert await scalar_count(workflow_context, outbox) == 0


async def test_expired_reclaim_racing_old_commit_never_deadlocks(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    claimed_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    stale = await workflow_context.service.claim_stage(worker_id="worker-a", now=claimed_at)
    assert stale is not None
    race_at = claimed_at + timedelta(seconds=31)

    commit_outcome, reclaim_outcome = await asyncio.wait_for(
        asyncio.gather(
            workflow_context.service.commit_stage(
                stale, completed_result(stale.stage), now=race_at
            ),
            workflow_context.service.claim_stage(worker_id="worker-b", now=race_at),
            return_exceptions=True,
        ),
        timeout=2,
    )

    if isinstance(commit_outcome, FenceViolationError):
        assert reclaim_outcome is not None
        assert not isinstance(reclaim_outcome, BaseException)
        assert reclaim_outcome.attempt_id == stale.attempt_id
        assert reclaim_outcome.fencing_token == stale.fencing_token + 1
    else:
        assert not isinstance(commit_outcome, BaseException)
        if reclaim_outcome is not None:
            assert not isinstance(reclaim_outcome, BaseException)
            assert reclaim_outcome.run_id == run_id
            assert reclaim_outcome.stage is StageName.EXTRACT


async def test_retry_skip_escalate_store_visible_reason_and_retry_uses_new_attempt(
    workflow_context: WorkflowTestContext,
) -> None:
    retry_run = await create_run(workflow_context, "sha256:retry")
    retry_claim = await workflow_context.service.claim_stage(
        worker_id="worker-a", now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    )
    assert retry_claim is not None and retry_claim.run_id == retry_run
    retry_at = datetime(2026, 8, 23, 12, 1, tzinfo=UTC)
    await workflow_context.service.commit_stage(
        retry_claim,
        StageResult(
            status="retry",
            artifact_hashes=(),
            next_stage=StageName.CLASSIFY,
            reason="provider timeout",
        ),
        retry_at=retry_at,
    )
    assert (
        await workflow_context.service.claim_stage(
            worker_id="worker-b", now=retry_at - timedelta(microseconds=1)
        )
        is None
    )
    retry_two = await workflow_context.service.claim_stage(worker_id="worker-b", now=retry_at)
    assert retry_two is not None
    assert retry_two.attempt_number == 2

    await workflow_context.service.commit_stage(
        retry_two,
        StageResult(
            status="skipped",
            artifact_hashes=(),
            next_stage=StageName.EXTRACT,
            reason="unsupported source",
        ),
    )
    extract = await workflow_context.service.claim_stage(worker_id="worker-b", now=retry_at)
    assert extract is not None and extract.stage is StageName.EXTRACT
    await workflow_context.service.commit_stage(
        extract,
        StageResult(
            status="escalated",
            artifact_hashes=(),
            next_stage=None,
            reason="ambiguous contract",
        ),
    )

    async with workflow_context.sessions() as session:
        rows = (
            await session.execute(
                select(stage_attempts.c.status, stage_attempts.c.failure)
                .where(stage_attempts.c.workflow_run_id == retry_run)
                .order_by(stage_attempts.c.created_at, stage_attempts.c.attempt_number)
            )
        ).all()
        run = (
            await session.execute(select(workflow_runs).where(workflow_runs.c.id == retry_run))
        ).mappings().one()
    assert [row.status for row in rows] == ["retry", "skipped", "escalated"]
    assert [row.failure["reason"] for row in rows] == [
        "provider timeout",
        "unsupported source",
        "ambiguous contract",
    ]
    assert run["status"] == "review_ready"
    assert run["failure"]["reason"] == "ambiguous contract"


async def test_invalidation_marks_only_affected_and_downstream_stages(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    for expected_stage in StageName:
        claim = await workflow_context.service.claim_stage(worker_id="worker-a", now=now)
        assert claim is not None and claim.stage is expected_stage
        await workflow_context.service.commit_stage(claim, completed_result(expected_stage))

    await workflow_context.service.invalidate_from(
        run_id=run_id,
        stage=StageName.RECONCILE,
        input_hash="sha256:changed-reconcile",
        reason="source dependency changed",
        now=now + timedelta(minutes=1),
    )
    claim = await workflow_context.service.claim_stage(
        worker_id="worker-b", now=now + timedelta(minutes=1)
    )
    assert claim is not None
    assert claim.stage is StageName.RECONCILE
    assert claim.attempt_number == 2
    async with workflow_context.sessions() as session:
        rows = (
            await session.execute(
                select(stage_attempts.c.stage, stage_attempts.c.invalidated_at)
                .where(stage_attempts.c.workflow_run_id == run_id)
                .where(stage_attempts.c.attempt_number == 1)
            )
        ).all()
    invalidated = {row.stage for row in rows if row.invalidated_at is not None}
    assert invalidated == {"reconcile", "check_rules", "draft"}


async def test_cancel_before_commit_recovers_same_attempt_and_completes_once(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    entered = asyncio.Event()
    executor_cancelled = asyncio.Event()
    heartbeat_entered = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()

    class BlockingHeartbeatService:
        async def claim_stage(self, **kwargs: object) -> object:
            return await workflow_context.service.claim_stage(**kwargs)  # type: ignore[arg-type]

        async def heartbeat_stage(self, *_args: object, **_kwargs: object) -> object:
            heartbeat_entered.set()
            try:
                await asyncio.Future()
            finally:
                heartbeat_cancelled.set()
            raise AssertionError("unreachable")

        async def fail_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.fail_stage(*args, **kwargs)  # type: ignore[arg-type]

        async def commit_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.commit_stage(*args, **kwargs)  # type: ignore[arg-type]

    async def cancelled_executor(_claim: object) -> StageExecution:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            executor_cancelled.set()
        raise AssertionError("unreachable")

    worker = asyncio.create_task(
        run_worker_iteration(
            BlockingHeartbeatService(),  # type: ignore[arg-type]
            worker_id="worker-a",
            execute_stage=cancelled_executor,
            clock=MutableClock(now),
            heartbeat_interval=0.001,
        )
    )
    await entered.wait()
    await heartbeat_entered.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert executor_cancelled.is_set()
    assert heartbeat_cancelled.is_set()

    async def successful_executor(claim: object) -> StageExecution:
        return StageExecution(result=completed_result(claim.stage))  # type: ignore[attr-defined]

    processed = await run_worker_iteration(
        workflow_context.service,
        worker_id="worker-b",
        execute_stage=successful_executor,
        clock=MutableClock(now + timedelta(seconds=31)),
        heartbeat_interval=3600,
    )

    assert processed is True
    async with workflow_context.sessions() as session:
        completed = await session.scalar(
            select(func.count())
            .select_from(stage_attempts)
            .where(stage_attempts.c.workflow_run_id == run_id)
            .where(stage_attempts.c.stage == StageName.CLASSIFY.value)
            .where(stage_attempts.c.status == AttemptStatus.COMPLETED.value)
        )
        attempts = await session.scalar(
            select(func.count())
            .select_from(stage_attempts)
            .where(stage_attempts.c.workflow_run_id == run_id)
            .where(stage_attempts.c.stage == StageName.CLASSIFY.value)
        )
    assert completed == 1
    assert attempts == 1


async def test_kill_after_commit_lost_response_does_not_repeat_completed_attempt(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    committed = asyncio.Event()

    class LostResponseService:
        async def claim_stage(self, **kwargs: object) -> object:
            return await workflow_context.service.claim_stage(**kwargs)  # type: ignore[arg-type]

        async def fail_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.fail_stage(*args, **kwargs)  # type: ignore[arg-type]

        async def heartbeat_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.heartbeat_stage(*args, **kwargs)  # type: ignore[arg-type]

        async def commit_stage(self, *args: object, **kwargs: object) -> object:
            await workflow_context.service.commit_stage(*args, **kwargs)  # type: ignore[arg-type]
            committed.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    async def successful_executor(claim: object) -> StageExecution:
        return StageExecution(result=completed_result(claim.stage))  # type: ignore[attr-defined]

    worker = asyncio.create_task(
        run_worker_iteration(
            LostResponseService(),  # type: ignore[arg-type]
            worker_id="worker-a",
            execute_stage=successful_executor,
            clock=MutableClock(now),
            heartbeat_interval=3600,
        )
    )
    await committed.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    restarted = WorkflowService(workflow_context.sessions, lease_duration=timedelta(seconds=30))
    next_claim = await restarted.claim_stage(worker_id="worker-b", now=now)

    assert next_claim is not None
    assert next_claim.stage is StageName.EXTRACT
    async with workflow_context.sessions() as session:
        completed = await session.scalar(
            select(func.count())
            .select_from(stage_attempts)
            .where(stage_attempts.c.workflow_run_id == run_id)
            .where(stage_attempts.c.stage == StageName.CLASSIFY.value)
            .where(stage_attempts.c.status == AttemptStatus.COMPLETED.value)
        )
    assert completed == 1


async def test_worker_records_retryable_exception_but_never_swallows_cancellation(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    failed_at = now + timedelta(seconds=17)
    clock = MutableClock(now)

    async def failing_executor(_claim: object) -> StageExecution:
        clock.current = failed_at
        raise RuntimeError("deterministic stage failed")

    assert await run_worker_iteration(
        workflow_context.service,
        worker_id="worker-a",
        execute_stage=failing_executor,
        clock=clock,
        heartbeat_interval=3600,
    )
    async with workflow_context.sessions() as session:
        attempt_row = (
            await session.execute(
                select(stage_attempts).where(stage_attempts.c.workflow_run_id == run_id)
            )
        ).mappings().one()
    failure = attempt_row["failure"]
    assert failure == {
        "reason": "deterministic stage failed",
        "retryable": True,
        "type": "RuntimeError",
    }
    assert attempt_row["completed_at"] == failed_at


async def test_long_execution_heartbeats_and_uses_post_execution_clock(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    started_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=25)
    clock = MutableClock(started_at)
    heartbeat_seen = asyncio.Event()
    heartbeat_count = 0

    class RecordingService:
        async def claim_stage(self, **kwargs: object) -> object:
            return await workflow_context.service.claim_stage(**kwargs)  # type: ignore[arg-type]

        async def heartbeat_stage(
            self, claim: ClaimedStage, **kwargs: object
        ) -> ClaimedStage:
            nonlocal heartbeat_count
            refreshed = await workflow_context.service.heartbeat_stage(
                claim, **kwargs  # type: ignore[arg-type]
            )
            heartbeat_count += 1
            heartbeat_seen.set()
            return refreshed

        async def fail_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.fail_stage(*args, **kwargs)  # type: ignore[arg-type]

        async def commit_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.commit_stage(*args, **kwargs)  # type: ignore[arg-type]

    async def long_executor(claim: ClaimedStage) -> StageExecution:
        clock.current = started_at + timedelta(seconds=20)
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=1)
        clock.current = finished_at
        return StageExecution(result=completed_result(claim.stage))

    assert await run_worker_iteration(
        RecordingService(),  # type: ignore[arg-type]
        worker_id="worker-a",
        execute_stage=long_executor,
        clock=clock,
        heartbeat_interval=0.001,
    )

    async with workflow_context.sessions() as session:
        attempt_row = (
            await session.execute(
                select(stage_attempts).where(stage_attempts.c.workflow_run_id == run_id)
            )
        ).mappings().one()
    assert heartbeat_count >= 1
    assert attempt_row["completed_at"] == finished_at


async def test_heartbeat_failure_is_visible_and_cancels_stage_execution(
    workflow_context: WorkflowTestContext,
) -> None:
    run_id = await create_run(workflow_context)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    executor_cancelled = asyncio.Event()

    class FailingHeartbeatService:
        async def claim_stage(self, **kwargs: object) -> object:
            return await workflow_context.service.claim_stage(**kwargs)  # type: ignore[arg-type]

        async def heartbeat_stage(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("heartbeat connection lost")

        async def fail_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.fail_stage(*args, **kwargs)  # type: ignore[arg-type]

        async def commit_stage(self, *args: object, **kwargs: object) -> object:
            return await workflow_context.service.commit_stage(*args, **kwargs)  # type: ignore[arg-type]

    async def blocked_executor(_claim: ClaimedStage) -> StageExecution:
        try:
            await asyncio.Future()
        finally:
            executor_cancelled.set()
        raise AssertionError("unreachable")

    with pytest.raises(RuntimeError, match="heartbeat connection lost"):
        await run_worker_iteration(
            FailingHeartbeatService(),  # type: ignore[arg-type]
            worker_id="worker-a",
            execute_stage=blocked_executor,
            clock=MutableClock(now),
            heartbeat_interval=0.001,
        )

    assert executor_cancelled.is_set()
    async with workflow_context.sessions() as session:
        status = await session.scalar(
            select(stage_attempts.c.status).where(stage_attempts.c.workflow_run_id == run_id)
        )
    assert status == AttemptStatus.RUNNING.value
    assert await scalar_count(workflow_context, audit_events) == 0
