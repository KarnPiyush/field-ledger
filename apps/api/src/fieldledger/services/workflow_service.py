from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.domain.models import DependencyRef, StageName, StageResult
from fieldledger.domain.workflow import (
    STAGE_ORDER,
    AttemptSnapshot,
    AttemptStatus,
    DecisionKind,
    InvalidTransitionError,
    RunSnapshot,
    RunStatus,
    next_stage,
    plan_next,
    validate_stage_result,
    validate_transition,
)
from fieldledger.persistence.repositories import (
    ClaimedStage,
    CommitConflictError,
    CommitReceipt,
    PreparedArtifact,
    PreparedDependency,
    WorkflowRepository,
    commit_payload,
)


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class WorkflowService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self.sessions = sessions
        self.lease_duration = lease_duration

    async def create_run(
        self,
        *,
        project_id: UUID,
        input_hash: str,
        manifest: tuple[DependencyRef, ...] = (),
        supersedes_run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> UUID:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        async with self.sessions.begin() as session:
            return await WorkflowRepository(session).create_run(
                project_id=project_id,
                input_hash=input_hash,
                stage_input_hashes={stage.value: input_hash for stage in STAGE_ORDER},
                manifest=manifest,
                supersedes_run_id=supersedes_run_id,
                now=timestamp,
            )

    async def create_incremental_run(
        self, *, project_id: UUID, now: datetime | None = None
    ) -> UUID | None:
        async with self.sessions.begin() as session:
            repository = WorkflowRepository(session)
            await repository.acquire_project_successor_lock(project_id)
            timestamp = _require_utc(now or datetime.now(UTC), "now")
            manifest = await repository.current_project_manifest(project_id)
            if not any(item.kind.value == "source_version" for item in manifest):
                return None
            canonical = "|".join(
                f"{item.kind.value}:{item.object_id}:{item.content_hash}"
                for item in sorted(
                    manifest,
                    key=lambda item: (item.kind.value, str(item.object_id)),
                )
            )
            input_hash = f"sha256:{sha256(canonical.encode()).hexdigest()}"
            existing = await repository.find_run_with_manifest(
                project_id=project_id,
                input_hash=input_hash,
                manifest=manifest,
            )
            if existing is not None:
                return existing
            predecessor = await repository.project_chain_tip_id(project_id)
            return await repository.create_run(
                project_id=project_id,
                input_hash=input_hash,
                stage_input_hashes={stage.value: input_hash for stage in STAGE_ORDER},
                manifest=manifest,
                supersedes_run_id=predecessor,
                now=timestamp,
            )

    async def snapshot(self, run_id: UUID, *, now: datetime | None = None) -> RunSnapshot:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        async with self.sessions() as session:
            repository = WorkflowRepository(session)
            run = await repository.get_run(run_id)
            attempts = await repository.attempts_for_run(run_id)
            return self._snapshot(run, attempts, timestamp)

    async def claim_stage(
        self,
        *,
        worker_id: str,
        run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ClaimedStage | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        lease_expires_at = timestamp + self.lease_duration
        async with self.sessions.begin() as session:
            repository = WorkflowRepository(session)
            excluded: set[UUID] = set()
            while run := await repository.lock_next_run(excluded, run_id=run_id):
                candidate_run_id = run["id"]
                excluded.add(candidate_run_id)
                attempts = await repository.attempts_for_run(candidate_run_id)
                decision = plan_next(self._snapshot(run, attempts, timestamp))
                if decision.kind in {DecisionKind.WAIT, DecisionKind.REVIEW_READY}:
                    if (
                        decision.kind is DecisionKind.REVIEW_READY
                        and run["status"] != RunStatus.REVIEW_READY.value
                    ):
                        await repository.mark_run_review_ready(candidate_run_id, timestamp)
                    continue
                if decision.stage is None or decision.attempt_number is None:
                    raise RuntimeError("claimable decision is missing stage data")
                if decision.kind is DecisionKind.RECLAIM:
                    if decision.attempt_id is None:
                        raise RuntimeError("reclaim decision is missing attempt ID")
                    attempt = await repository.reclaim_attempt(
                        attempt_id=decision.attempt_id,
                        worker_id=worker_id,
                        now=timestamp,
                        lease_expires_at=lease_expires_at,
                    )
                else:
                    attempt = await repository.insert_attempt(
                        run_id=candidate_run_id,
                        stage=decision.stage,
                        attempt_number=decision.attempt_number,
                        input_hash=decision.input_hash or run["input_hash"],
                        worker_id=worker_id,
                        now=timestamp,
                        lease_expires_at=lease_expires_at,
                    )
                await repository.update_run_claimed(candidate_run_id, decision.stage, timestamp)
                return self._claim(attempt)
        return None

    async def heartbeat_stage(
        self, claim: ClaimedStage, *, now: datetime | None = None
    ) -> ClaimedStage:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        async with self.sessions.begin() as session:
            row = await WorkflowRepository(session).heartbeat_claim(
                claim, timestamp + self.lease_duration
            )
            return self._claim(row)

    async def commit_stage(
        self,
        claim: ClaimedStage,
        result: StageResult,
        *,
        artifacts: tuple[PreparedArtifact, ...] = (),
        dependencies: tuple[PreparedDependency, ...] = (),
        duration_ms: int | None = None,
        estimated_cost: Decimal | None = None,
        retry_at: datetime | None = None,
        now: datetime | None = None,
        failure_details: dict[str, Any] | None = None,
    ) -> CommitReceipt:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        normalized_retry_at = _require_utc(retry_at, "retry_at") if retry_at else None
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms cannot be negative")
        validate_stage_result(claim.stage, result)
        artifact_hashes = tuple(item.hash for item in artifacts)
        if artifact_hashes != result.artifact_hashes:
            raise InvalidTransitionError(
                "stage result artifact hashes must match prepared artifacts"
            )
        if any(item.artifact_hash not in artifact_hashes for item in dependencies):
            raise InvalidTransitionError("dependencies must refer to prepared artifacts")
        status = AttemptStatus(result.status)
        failure = failure_details
        if failure is None and result.reason is not None:
            failure = {
                "reason": result.reason,
                "retryable": status is AttemptStatus.RETRY,
            }
        payload = commit_payload(
            result=result.model_dump(mode="json"),
            artifacts=artifacts,
            dependencies=dependencies,
            duration_ms=duration_ms,
            estimated_cost=str(estimated_cost) if estimated_cost is not None else None,
            retry_at=normalized_retry_at,
            failure=failure,
        )

        async with self.sessions.begin() as session:
            repository = WorkflowRepository(session)
            run, attempt = await repository.lock_run_and_assert_fence(claim)
            if attempt["status"] != AttemptStatus.RUNNING.value:
                if attempt["result"] == payload and attempt["status"] == status.value:
                    return CommitReceipt(claim.run_id, claim.attempt_id, status, True)
                raise CommitConflictError("attempt already has a different terminal result")
            validate_transition(AttemptStatus(attempt["status"]), status)
            artifact_link_ids = await repository.put_artifacts(
                claim=claim, artifacts=artifacts
            )
            await repository.replace_dependencies(
                artifact_link_ids=artifact_link_ids, dependencies=dependencies
            )
            if claim.stage is StageName.EXTRACT and status is AttemptStatus.COMPLETED:
                await repository.put_cited_facts(
                    run_id=claim.run_id,
                    artifacts=artifacts,
                )
            await repository.complete_attempt(
                claim=claim,
                status=status,
                now=timestamp,
                duration_ms=duration_ms,
                estimated_cost=estimated_cost,
                failure=failure,
                result_payload=payload,
                retry_at=normalized_retry_at,
            )
            await repository.complete_run(
                claim.run_id, self._run_values(claim, result, timestamp, failure)
            )
            await repository.append_stage_events(
                project_id=run["project_id"],
                claim=claim,
                status=status,
                result=result,
                now=timestamp,
            )
        return CommitReceipt(claim.run_id, claim.attempt_id, status, False)

    async def fail_stage(
        self,
        claim: ClaimedStage,
        error: Exception,
        *,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CommitReceipt:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        result = StageResult(
            status="retry",
            artifact_hashes=(),
            next_stage=claim.stage,
            reason=str(error) or type(error).__name__,
        )
        return await self.commit_stage(
            claim,
            result,
            retry_at=retry_at or timestamp,
            now=timestamp,
            failure_details={
                "reason": str(error) or type(error).__name__,
                "retryable": True,
                "type": type(error).__name__,
            },
        )

    async def invalidate_from(
        self,
        *,
        run_id: UUID,
        stage: StageName,
        input_hash: str,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        timestamp = _require_utc(now or datetime.now(UTC), "now")
        affected = tuple(item.value for item in STAGE_ORDER[STAGE_ORDER.index(stage) :])
        async with self.sessions.begin() as session:
            repository = WorkflowRepository(session)
            run = await repository.lock_run(run_id)
            if run is None:
                raise LookupError(f"workflow run not found: {run_id}")
            attempts = await repository.attempts_for_run(run_id)
            if any(
                item["stage"] in affected and item["status"] == AttemptStatus.RUNNING.value
                for item in attempts
            ):
                raise InvalidTransitionError("cannot invalidate a running affected stage")
            latest_target = max(
                (item for item in attempts if item["stage"] == stage.value),
                key=lambda item: item["attempt_number"],
                default=None,
            )
            if (
                latest_target is not None
                and latest_target["input_hash"] == input_hash
                and latest_target["invalidated_at"] is None
            ):
                return False
            latest_by_stage: dict[str, Any] = {}
            for item in attempts:
                current = latest_by_stage.get(item["stage"])
                if current is None or item["attempt_number"] > current["attempt_number"]:
                    latest_by_stage[item["stage"]] = item
            ids = [
                item["id"]
                for stage_name, item in latest_by_stage.items()
                if stage_name in affected
                and item["status"]
                in {AttemptStatus.COMPLETED.value, AttemptStatus.SKIPPED.value}
            ]
            await repository.invalidate_attempts(ids, now=timestamp, reason=reason)
            stage_inputs = dict(run["stage_input_hashes"])
            stage_inputs[stage.value] = input_hash
            await repository.invalidate_run(
                run_id=run_id,
                stage=stage,
                stage_input_hashes=stage_inputs,
                reason=reason,
                now=timestamp,
            )
            await repository.append_invalidation_events(
                project_id=run["project_id"],
                run_id=run_id,
                stage=stage,
                reason=reason,
                now=timestamp,
            )
        return True

    @staticmethod
    def _snapshot(run: Any, attempts: tuple[Any, ...], now: datetime) -> RunSnapshot:
        return RunSnapshot(
            run_id=run["id"],
            status=RunStatus(run["status"]),
            input_hashes=run["stage_input_hashes"],
            attempts=tuple(
                AttemptSnapshot(
                    attempt_id=item["id"],
                    stage=StageName(item["stage"]),
                    attempt_number=item["attempt_number"],
                    status=AttemptStatus(item["status"]),
                    input_hash=item["input_hash"],
                    lease_owner=item["lease_owner"],
                    lease_expires_at=item["lease_expires_at"],
                    fencing_token=item["fencing_token"],
                    next_attempt_at=item["next_attempt_at"],
                    invalidated_at=item["invalidated_at"],
                )
                for item in attempts
            ),
            now=now,
        )

    @staticmethod
    def _claim(row: Any) -> ClaimedStage:
        return ClaimedStage(
            run_id=row["workflow_run_id"],
            attempt_id=row["id"],
            stage=StageName(row["stage"]),
            attempt_number=row["attempt_number"],
            input_hash=row["input_hash"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            fencing_token=row["fencing_token"],
        )

    @staticmethod
    def _run_values(
        claim: ClaimedStage,
        result: StageResult,
        now: datetime,
        failure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if result.status in {"completed", "skipped"}:
            following = next_stage(claim.stage)
            return {
                "status": "review_ready" if following is None else "pending",
                "current_stage": following.value if following else None,
                "failure": None,
                "completed_at": now if following is None else None,
                "updated_at": now,
            }
        if result.status == "retry":
            return {
                "status": "pending",
                "current_stage": claim.stage.value,
                "failure": failure,
                "completed_at": None,
                "updated_at": now,
            }
        return {
            "status": "review_ready",
            "current_stage": claim.stage.value,
            "failure": failure,
            "completed_at": None,
            "updated_at": now,
        }
