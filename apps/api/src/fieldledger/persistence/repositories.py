import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    RowMapping,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fieldledger.domain.models import (
    ArtifactKind,
    DependencyKind,
    DependencyRef,
    StageName,
    StageResult,
)
from fieldledger.domain.workflow import AttemptStatus, RunStatus
from fieldledger.persistence.tables import (
    artifact_blobs,
    audit_events,
    citations,
    document_versions,
    documents,
    facts,
    outbox,
    rule_sets,
    run_input_manifest,
    stage_artifacts,
    stage_attempts,
    stage_dependencies,
    workflow_runs,
)


class FenceViolationError(RuntimeError):
    pass


class ArtifactConflictError(RuntimeError):
    pass


class CommitConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedArtifact:
    hash: str
    kind: ArtifactKind
    media_type: str
    content: bytes
    storage_uri: str | None = None

    def __post_init__(self) -> None:
        if self.storage_uri is None:
            object.__setattr__(self, "storage_uri", f"postgresql://artifact-blobs/{self.hash}")


@dataclass(frozen=True)
class PreparedDependency:
    artifact_hash: str
    kind: DependencyKind
    dependency_key: str
    content_hash: str


@dataclass(frozen=True)
class ClaimedStage:
    run_id: UUID
    attempt_id: UUID
    stage: StageName
    attempt_number: int
    input_hash: str
    lease_owner: str
    lease_expires_at: datetime
    fencing_token: int


@dataclass(frozen=True)
class CommitReceipt:
    run_id: UUID
    attempt_id: UUID
    status: AttemptStatus
    replayed: bool


class WorkflowRepository:
    """All SQL used by one workflow transaction, with run-before-attempt locking."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        *,
        project_id: UUID,
        input_hash: str,
        stage_input_hashes: dict[str, str],
        manifest: tuple[DependencyRef, ...],
        supersedes_run_id: UUID | None,
        now: datetime,
    ) -> UUID:
        run_id = (
            await self.session.execute(
                insert(workflow_runs)
                .values(
                    project_id=project_id,
                    status=RunStatus.PENDING.value,
                    current_stage=StageName.CLASSIFY.value,
                    input_hash=input_hash,
                    supersedes_run_id=supersedes_run_id,
                    stage_input_hashes=stage_input_hashes,
                    created_at=now,
                    updated_at=now,
                )
                .returning(workflow_runs.c.id)
            )
        ).scalar_one()
        if manifest:
            await self.session.execute(
                insert(run_input_manifest),
                [
                    {
                        "workflow_run_id": run_id,
                        "kind": item.kind.value,
                        "object_id": item.object_id,
                        "content_hash": item.content_hash,
                        "created_at": now,
                    }
                    for item in manifest
                ],
            )
        return cast(UUID, run_id)

    async def acquire_project_successor_lock(self, project_id: UUID) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"successor:{project_id}"},
        )

    async def current_project_manifest(
        self, project_id: UUID
    ) -> tuple[DependencyRef, ...]:
        latest = (
            select(
                document_versions.c.document_id,
                func.max(document_versions.c.version_number).label("version_number"),
            )
            .group_by(document_versions.c.document_id)
            .subquery()
        )
        versions = (
            await self.session.execute(
                select(document_versions.c.id, document_versions.c.content_hash)
                .select_from(
                    document_versions.join(documents).join(
                        latest,
                        (latest.c.document_id == document_versions.c.document_id)
                        & (latest.c.version_number == document_versions.c.version_number),
                    )
                )
                .where(documents.c.project_id == project_id)
            )
        ).all()
        rule = (
            await self.session.execute(
                select(rule_sets.c.id, rule_sets.c.content_hash)
                .where(rule_sets.c.project_id == project_id)
                .order_by(rule_sets.c.version_number.desc())
                .limit(1)
            )
        ).one_or_none()
        manifest = [
            DependencyRef(
                kind=DependencyKind.SOURCE_VERSION,
                object_id=row.id,
                content_hash=row.content_hash,
            )
            for row in versions
        ]
        if rule is not None:
            manifest.append(
                DependencyRef(
                    kind=DependencyKind.RULE_SET_VERSION,
                    object_id=rule.id,
                    content_hash=rule.content_hash,
                )
            )
        return tuple(manifest)

    async def project_chain_tip_id(self, project_id: UUID) -> UUID | None:
        child = workflow_runs.alias("child")
        value = (
            await self.session.execute(
                select(workflow_runs.c.id)
                .select_from(
                    workflow_runs.outerjoin(
                        child,
                        child.c.supersedes_run_id == workflow_runs.c.id,
                    )
                )
                .where(workflow_runs.c.project_id == project_id)
                .where(child.c.id.is_(None))
            )
        ).scalar_one_or_none()
        return cast(UUID | None, value)

    async def find_run_with_manifest(
        self,
        *,
        project_id: UUID,
        input_hash: str,
        manifest: tuple[DependencyRef, ...],
    ) -> UUID | None:
        candidate_ids = (
            await self.session.execute(
                select(workflow_runs.c.id)
                .where(workflow_runs.c.project_id == project_id)
                .where(workflow_runs.c.input_hash == input_hash)
                .order_by(workflow_runs.c.created_at.desc(), workflow_runs.c.id.desc())
            )
        ).scalars()
        expected = tuple(
            sorted(
                (item.kind.value, str(item.object_id), item.content_hash)
                for item in manifest
            )
        )
        for candidate_id in candidate_ids:
            rows = (
                await self.session.execute(
                    select(
                        run_input_manifest.c.kind,
                        run_input_manifest.c.object_id,
                        run_input_manifest.c.content_hash,
                    ).where(run_input_manifest.c.workflow_run_id == candidate_id)
                )
            ).all()
            actual = tuple(
                sorted((row.kind, str(row.object_id), row.content_hash) for row in rows)
            )
            if actual == expected:
                return cast(UUID, candidate_id)
        return None

    async def get_run(self, run_id: UUID) -> RowMapping:
        return (
            await self.session.execute(select(workflow_runs).where(workflow_runs.c.id == run_id))
        ).mappings().one()

    async def lock_next_run(
        self,
        excluded_run_ids: set[UUID],
        *,
        run_id: UUID | None = None,
    ) -> RowMapping | None:
        predecessor = workflow_runs.alias("predecessor")
        reusable_snapshot = exists(
            select(stage_artifacts.c.id).where(
                stage_artifacts.c.workflow_run_id == predecessor.c.id,
                stage_artifacts.c.kind == ArtifactKind.ANALYSIS_SNAPSHOT.value,
            )
        )
        statement = (
            select(workflow_runs)
            .select_from(
                workflow_runs.outerjoin(
                    predecessor,
                    workflow_runs.c.supersedes_run_id == predecessor.c.id,
                )
            )
            .where(workflow_runs.c.status.in_(("pending", "running")))
            .where(
                or_(
                    workflow_runs.c.supersedes_run_id.is_(None),
                    and_(
                        predecessor.c.status == RunStatus.REVIEW_READY.value,
                        or_(
                            predecessor.c.failure.is_not(None),
                            reusable_snapshot,
                        ),
                    ),
                )
            )
            .order_by(workflow_runs.c.created_at, workflow_runs.c.id)
            .limit(1)
            .with_for_update(of=workflow_runs, skip_locked=True)
        )
        if excluded_run_ids:
            statement = statement.where(workflow_runs.c.id.not_in(excluded_run_ids))
        if run_id is not None:
            statement = statement.where(workflow_runs.c.id == run_id)
        return (await self.session.execute(statement)).mappings().one_or_none()

    async def lock_run(self, run_id: UUID) -> RowMapping | None:
        return (
            await self.session.execute(
                select(workflow_runs)
                .where(workflow_runs.c.id == run_id)
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def attempts_for_run(self, run_id: UUID) -> tuple[RowMapping, ...]:
        result = await self.session.execute(
            select(stage_attempts)
            .where(stage_attempts.c.workflow_run_id == run_id)
            .order_by(stage_attempts.c.stage, stage_attempts.c.attempt_number)
        )
        return tuple(result.mappings())

    async def lock_attempt(self, attempt_id: UUID) -> RowMapping | None:
        return (
            await self.session.execute(
                select(stage_attempts)
                .where(stage_attempts.c.id == attempt_id)
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def lock_run_and_assert_fence(
        self, claim: ClaimedStage
    ) -> tuple[RowMapping, RowMapping]:
        run = await self.lock_run(claim.run_id)
        if run is None:
            raise FenceViolationError("workflow run does not exist")
        attempt = await self.lock_attempt(claim.attempt_id)
        if attempt is None:
            raise FenceViolationError("attempt does not exist")
        if (
            attempt["workflow_run_id"] != claim.run_id
            or attempt["stage"] != claim.stage.value
            or attempt["lease_owner"] != claim.lease_owner
            or attempt["fencing_token"] != claim.fencing_token
        ):
            raise FenceViolationError("attempt owner or fencing token is stale")
        return run, attempt

    async def insert_attempt(
        self,
        *,
        run_id: UUID,
        stage: StageName,
        attempt_number: int,
        input_hash: str,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> RowMapping:
        return (
            await self.session.execute(
                insert(stage_attempts)
                .values(
                    workflow_run_id=run_id,
                    stage=stage.value,
                    attempt_number=attempt_number,
                    status=AttemptStatus.RUNNING.value,
                    input_hash=input_hash,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    fencing_token=1,
                    started_at=now,
                )
                .returning(stage_attempts)
            )
        ).mappings().one()

    async def reclaim_attempt(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> RowMapping:
        row = (
            await self.session.execute(
                update(stage_attempts)
                .where(stage_attempts.c.id == attempt_id)
                .where(stage_attempts.c.status == AttemptStatus.RUNNING.value)
                .where(stage_attempts.c.lease_expires_at <= now)
                .values(
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    fencing_token=stage_attempts.c.fencing_token + 1,
                    started_at=now,
                )
                .returning(stage_attempts)
            )
        ).mappings().one_or_none()
        if row is None:
            raise FenceViolationError("attempt lease is no longer reclaimable")
        return row

    async def update_run_claimed(self, run_id: UUID, stage: StageName, now: datetime) -> None:
        await self.session.execute(
            update(workflow_runs)
            .where(workflow_runs.c.id == run_id)
            .values(status="running", current_stage=stage.value, updated_at=now, failure=None)
        )

    async def mark_run_review_ready(self, run_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(workflow_runs)
            .where(workflow_runs.c.id == run_id)
            .values(status="review_ready", current_stage=None, updated_at=now)
        )

    async def heartbeat_claim(
        self, claim: ClaimedStage, lease_expires_at: datetime
    ) -> RowMapping:
        _, attempt = await self.lock_run_and_assert_fence(claim)
        if attempt["status"] != AttemptStatus.RUNNING.value:
            raise FenceViolationError("only a running attempt can be heartbeated")
        return (
            await self.session.execute(
                update(stage_attempts)
                .where(stage_attempts.c.id == claim.attempt_id)
                .values(lease_expires_at=lease_expires_at)
                .returning(stage_attempts)
            )
        ).mappings().one()

    async def put_artifacts(
        self,
        *,
        claim: ClaimedStage,
        artifacts: tuple[PreparedArtifact, ...],
    ) -> dict[str, UUID]:
        link_ids: dict[str, UUID] = {}
        for artifact in artifacts:
            digest = f"sha256:{sha256(artifact.content).hexdigest()}"
            if digest != artifact.hash:
                raise ArtifactConflictError(
                    f"artifact content does not match its address: {artifact.hash}"
                )
            await self.session.execute(
                postgres_insert(artifact_blobs)
                .values(
                    artifact_hash=artifact.hash,
                    media_type=artifact.media_type,
                    storage_uri=artifact.storage_uri,
                    byte_size=len(artifact.content),
                    content=artifact.content,
                )
                .on_conflict_do_nothing(index_elements=[artifact_blobs.c.artifact_hash])
            )
            blob = (
                await self.session.execute(
                    select(artifact_blobs).where(
                        artifact_blobs.c.artifact_hash == artifact.hash
                    )
                )
            ).mappings().one()
            immutable_values = (
                blob["media_type"],
                blob["storage_uri"],
                blob["byte_size"],
                blob["content"],
            )
            proposed_values = (
                artifact.media_type,
                artifact.storage_uri,
                len(artifact.content),
                artifact.content,
            )
            if immutable_values != proposed_values:
                raise ArtifactConflictError(
                    f"artifact hash {artifact.hash} already names different immutable content"
                )
            await self.session.execute(
                postgres_insert(stage_artifacts)
                .values(
                    workflow_run_id=claim.run_id,
                    stage_attempt_id=claim.attempt_id,
                    artifact_blob_id=blob["id"],
                    kind=artifact.kind.value,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        stage_artifacts.c.stage_attempt_id,
                        stage_artifacts.c.artifact_blob_id,
                        stage_artifacts.c.kind,
                    ]
                )
            )
            link = (
                await self.session.execute(
                    select(stage_artifacts).where(
                        stage_artifacts.c.stage_attempt_id == claim.attempt_id,
                        stage_artifacts.c.artifact_blob_id == blob["id"],
                        stage_artifacts.c.kind == artifact.kind.value,
                    )
                )
            ).mappings().one()
            link_ids[artifact.hash] = link["id"]
        return link_ids

    async def replace_dependencies(
        self,
        *,
        artifact_link_ids: dict[str, UUID],
        dependencies: tuple[PreparedDependency, ...],
    ) -> None:
        grouped: dict[str, list[PreparedDependency]] = {
            artifact_hash: [] for artifact_hash in artifact_link_ids
        }
        for dependency in dependencies:
            if dependency.artifact_hash not in artifact_link_ids:
                raise ArtifactConflictError(
                    f"dependency references an uncommitted artifact: {dependency.artifact_hash}"
                )
            grouped[dependency.artifact_hash].append(dependency)

        for artifact_hash, link_id in artifact_link_ids.items():
            await self.session.execute(
                delete(stage_dependencies).where(
                    stage_dependencies.c.stage_artifact_id == link_id
                )
            )
            rows = [
                {
                    "stage_artifact_id": link_id,
                    "kind": dependency.kind.value,
                    "dependency_key": dependency.dependency_key,
                    "content_hash": dependency.content_hash,
                }
                for dependency in grouped[artifact_hash]
            ]
            if rows:
                await self.session.execute(insert(stage_dependencies), rows)

    async def put_cited_facts(
        self,
        *,
        run_id: UUID,
        artifacts: tuple[PreparedArtifact, ...],
    ) -> None:
        for artifact in artifacts:
            if artifact.kind is not ArtifactKind.FACT_SET:
                continue
            envelope = json.loads(artifact.content)
            payload = envelope["payload"]
            citation = payload["citation"]
            await self.session.execute(
                postgres_insert(facts)
                .values(
                    workflow_run_id=run_id,
                    fact_key=payload["fact_key"],
                    value=payload["value"],
                    unit=payload.get("unit"),
                    supported=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[facts.c.workflow_run_id, facts.c.fact_key]
                )
            )
            fact_id = await self.session.scalar(
                select(facts.c.id).where(
                    facts.c.workflow_run_id == run_id,
                    facts.c.fact_key == payload["fact_key"],
                )
            )
            await self.session.execute(
                postgres_insert(citations)
                .values(
                    fact_id=fact_id,
                    source_chunk_id=UUID(citation["source_chunk_id"]),
                    coordinate=citation["coordinate"],
                    quoted_text=citation["quoted_text"],
                )
                .on_conflict_do_nothing(
                    index_elements=[citations.c.fact_id, citations.c.source_chunk_id]
                )
            )

    async def complete_attempt(
        self,
        *,
        claim: ClaimedStage,
        status: AttemptStatus,
        now: datetime,
        duration_ms: int | None,
        estimated_cost: Decimal | None,
        failure: dict[str, Any] | None,
        result_payload: dict[str, Any],
        retry_at: datetime | None,
    ) -> None:
        await self.session.execute(
            update(stage_attempts)
            .where(stage_attempts.c.id == claim.attempt_id)
            .values(
                status=status.value,
                lease_expires_at=None,
                completed_at=now,
                duration_ms=duration_ms,
                estimated_cost=estimated_cost,
                failure=failure,
                result=result_payload,
                next_attempt_at=retry_at,
            )
        )

    async def complete_run(self, run_id: UUID, values: dict[str, Any]) -> None:
        await self.session.execute(
            update(workflow_runs).where(workflow_runs.c.id == run_id).values(**values)
        )

    async def append_stage_events(
        self,
        *,
        project_id: UUID,
        claim: ClaimedStage,
        status: AttemptStatus,
        result: StageResult,
        now: datetime,
    ) -> None:
        await self.session.execute(
            insert(audit_events).values(
                project_id=project_id,
                actor_id=claim.lease_owner,
                actor_source="worker",
                event_type=f"workflow.stage_{status.value}",
                aggregate_type="workflow_run",
                aggregate_id=claim.run_id,
                artifact_hashes=list(result.artifact_hashes),
                payload={
                    "attempt_id": str(claim.attempt_id),
                    "stage": claim.stage.value,
                    "attempt_number": claim.attempt_number,
                    "reason": result.reason,
                },
                created_at=now,
            )
        )
        await self.session.execute(
            insert(outbox).values(
                topic="workflow.stage_committed",
                aggregate_type="workflow_run",
                aggregate_id=claim.run_id,
                payload={
                    "attempt_id": str(claim.attempt_id),
                    "stage": claim.stage.value,
                    "status": status.value,
                },
                created_at=now,
            )
        )

    async def invalidate_attempts(
        self, attempt_ids: list[UUID], *, now: datetime, reason: str
    ) -> None:
        if attempt_ids:
            await self.session.execute(
                update(stage_attempts)
                .where(stage_attempts.c.id.in_(attempt_ids))
                .values(invalidated_at=now, invalidation_reason=reason)
            )

    async def invalidate_run(
        self,
        *,
        run_id: UUID,
        stage: StageName,
        stage_input_hashes: dict[str, str],
        reason: str,
        now: datetime,
    ) -> None:
        await self.session.execute(
            update(workflow_runs)
            .where(workflow_runs.c.id == run_id)
            .values(
                status=RunStatus.PENDING.value,
                current_stage=stage.value,
                stage_input_hashes=stage_input_hashes,
                failure={"reason": reason, "invalidated": True},
                completed_at=None,
                updated_at=now,
            )
        )

    async def append_invalidation_events(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        stage: StageName,
        reason: str,
        now: datetime,
    ) -> None:
        await self.session.execute(
            insert(audit_events).values(
                project_id=project_id,
                actor_id="workflow-service",
                actor_source="system",
                event_type="workflow.invalidated",
                aggregate_type="workflow_run",
                aggregate_id=run_id,
                payload={"stage": stage.value, "reason": reason},
                created_at=now,
            )
        )
        await self.session.execute(
            insert(outbox).values(
                topic="workflow.invalidated",
                aggregate_type="workflow_run",
                aggregate_id=run_id,
                payload={"stage": stage.value},
                created_at=now,
            )
        )


def commit_payload(
    *,
    result: dict[str, Any],
    artifacts: tuple[PreparedArtifact, ...],
    dependencies: tuple[PreparedDependency, ...],
    duration_ms: int | None,
    estimated_cost: str | None,
    retry_at: datetime | None,
    failure: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "stage_result": result,
        "artifacts": [
            {
                "hash": artifact.hash,
                "kind": artifact.kind.value,
                "media_type": artifact.media_type,
                "storage_uri": artifact.storage_uri,
                "size": len(artifact.content),
                "content_digest": sha256(artifact.content).hexdigest(),
            }
            for artifact in artifacts
        ],
        "dependencies": [
            {
                "artifact_hash": dependency.artifact_hash,
                "kind": dependency.kind.value,
                "dependency_key": dependency.dependency_key,
                "content_hash": dependency.content_hash,
            }
            for dependency in dependencies
        ],
        "duration_ms": duration_ms,
        "estimated_cost": estimated_cost,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "failure": failure,
    }
