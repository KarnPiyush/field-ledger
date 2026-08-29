from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from pydantic import Field

from fieldledger.domain.models import ContractModel, StageName, StageResult

STAGE_ORDER = (
    StageName.CLASSIFY,
    StageName.EXTRACT,
    StageName.RECONCILE,
    StageName.CHECK_RULES,
    StageName.DRAFT,
)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEW_READY = "review_ready"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    RETRY = "retry"
    ESCALATED = "escalated"


class DecisionKind(StrEnum):
    WAIT = "wait"
    CLAIM = "claim"
    RECLAIM = "reclaim"
    RETRY = "retry"
    REVIEW_READY = "review_ready"


class InvalidTransitionError(ValueError):
    pass


class AttemptSnapshot(ContractModel):
    attempt_id: UUID
    stage: StageName
    attempt_number: int = Field(ge=1)
    status: AttemptStatus
    input_hash: str
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    fencing_token: int = Field(ge=0)
    next_attempt_at: datetime | None = None
    invalidated_at: datetime | None = None


class RunSnapshot(ContractModel):
    run_id: UUID
    status: RunStatus
    input_hashes: dict[StageName, str]
    attempts: tuple[AttemptSnapshot, ...]
    now: datetime


class StageDecision(ContractModel):
    kind: DecisionKind
    stage: StageName | None = None
    attempt_id: UUID | None = None
    attempt_number: int | None = Field(default=None, ge=1)
    input_hash: str | None = None
    reason: str | None = None


_TRANSITIONS = MappingProxyType(
    {
        AttemptStatus.PENDING: frozenset({AttemptStatus.RUNNING}),
        AttemptStatus.RUNNING: frozenset(
            {
                AttemptStatus.RUNNING,
                AttemptStatus.COMPLETED,
                AttemptStatus.SKIPPED,
                AttemptStatus.RETRY,
                AttemptStatus.ESCALATED,
            }
        ),
        AttemptStatus.COMPLETED: frozenset(),
        AttemptStatus.SKIPPED: frozenset(),
        AttemptStatus.RETRY: frozenset(),
        AttemptStatus.ESCALATED: frozenset(),
    }
)


def validate_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"invalid attempt transition: {current.value} -> {target.value}"
        )


def next_stage(stage: StageName) -> StageName | None:
    index = STAGE_ORDER.index(stage)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None


def validate_stage_result(stage: StageName, result: StageResult) -> None:
    if result.status != AttemptStatus.COMPLETED.value and not (result.reason or "").strip():
        raise InvalidTransitionError(f"{result.status} result requires a nonblank reason")
    expected = stage if result.status == AttemptStatus.RETRY.value else None
    if result.status in {AttemptStatus.COMPLETED.value, AttemptStatus.SKIPPED.value}:
        expected = next_stage(stage)
    if result.next_stage is not expected:
        rendered = result.next_stage.value if result.next_stage else None
        expected_value = expected.value if expected else None
        raise InvalidTransitionError(
            f"{result.status} result for {stage.value} must point to "
            f"{expected_value}, got {rendered}"
        )


def plan_next(snapshot: RunSnapshot) -> StageDecision:
    latest: dict[StageName, AttemptSnapshot] = {}
    for item in snapshot.attempts:
        previous = latest.get(item.stage)
        if previous is None or item.attempt_number > previous.attempt_number:
            latest[item.stage] = item

    for stage in STAGE_ORDER:
        stage_attempt = latest.get(stage)
        expected_hash = snapshot.input_hashes[stage]
        if stage_attempt is None:
            return StageDecision(
                kind=DecisionKind.CLAIM,
                stage=stage,
                attempt_number=1,
                input_hash=expected_hash,
            )
        if stage_attempt.status is AttemptStatus.RUNNING:
            if (
                stage_attempt.lease_expires_at is not None
                and stage_attempt.lease_expires_at > snapshot.now
            ):
                return StageDecision(
                    kind=DecisionKind.WAIT,
                    stage=stage,
                    attempt_id=stage_attempt.attempt_id,
                    attempt_number=stage_attempt.attempt_number,
                    input_hash=stage_attempt.input_hash,
                    reason="live lease",
                )
            return StageDecision(
                kind=DecisionKind.RECLAIM,
                stage=stage,
                attempt_id=stage_attempt.attempt_id,
                attempt_number=stage_attempt.attempt_number,
                input_hash=stage_attempt.input_hash,
                reason="expired lease",
            )
        if stage_attempt.status is AttemptStatus.RETRY:
            if (
                stage_attempt.next_attempt_at is not None
                and stage_attempt.next_attempt_at > snapshot.now
            ):
                return StageDecision(
                    kind=DecisionKind.WAIT,
                    stage=stage,
                    attempt_id=stage_attempt.attempt_id,
                    attempt_number=stage_attempt.attempt_number,
                    input_hash=stage_attempt.input_hash,
                    reason="retry not due",
                )
            return StageDecision(
                kind=DecisionKind.RETRY,
                stage=stage,
                attempt_number=stage_attempt.attempt_number + 1,
                input_hash=expected_hash,
                reason="retry due",
            )
        if stage_attempt.status is AttemptStatus.ESCALATED:
            return StageDecision(
                kind=DecisionKind.REVIEW_READY,
                stage=stage,
                reason="escalated",
            )
        if (
            stage_attempt.status not in {AttemptStatus.COMPLETED, AttemptStatus.SKIPPED}
            or stage_attempt.input_hash != expected_hash
            or stage_attempt.invalidated_at is not None
        ):
            return StageDecision(
                kind=DecisionKind.CLAIM,
                stage=stage,
                attempt_number=stage_attempt.attempt_number + 1,
                input_hash=expected_hash,
                reason="missing or invalidated",
            )

    return StageDecision(kind=DecisionKind.REVIEW_READY)
