from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

import pytest
from fieldledger.domain.models import StageName, StageResult
from fieldledger.domain.workflow import (
    AttemptSnapshot,
    AttemptStatus,
    DecisionKind,
    InvalidTransitionError,
    RunSnapshot,
    RunStatus,
    plan_next,
    validate_stage_result,
    validate_transition,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
INPUTS = {stage: f"sha256:{stage.value}" for stage in StageName}
VALID_TRANSITIONS = {
    (AttemptStatus.PENDING, AttemptStatus.RUNNING),
    (AttemptStatus.RUNNING, AttemptStatus.RUNNING),
    (AttemptStatus.RUNNING, AttemptStatus.COMPLETED),
    (AttemptStatus.RUNNING, AttemptStatus.SKIPPED),
    (AttemptStatus.RUNNING, AttemptStatus.RETRY),
    (AttemptStatus.RUNNING, AttemptStatus.ESCALATED),
}
INVALID_TRANSITIONS = tuple(
    edge for edge in product(AttemptStatus, repeat=2) if edge not in VALID_TRANSITIONS
)


def attempt(
    stage: StageName,
    status: AttemptStatus,
    *,
    number: int = 1,
    input_hash: str | None = None,
    lease_expires_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
    invalidated_at: datetime | None = None,
) -> AttemptSnapshot:
    return AttemptSnapshot(
        attempt_id=uuid4(),
        stage=stage,
        attempt_number=number,
        status=status,
        input_hash=input_hash or INPUTS[stage],
        lease_owner="worker-a" if status is AttemptStatus.RUNNING else None,
        lease_expires_at=lease_expires_at,
        fencing_token=3,
        next_attempt_at=next_attempt_at,
        invalidated_at=invalidated_at,
    )


def snapshot(*attempts: AttemptSnapshot) -> RunSnapshot:
    return RunSnapshot(
        run_id=uuid4(),
        status=RunStatus.PENDING,
        input_hashes=INPUTS,
        attempts=attempts,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("current", "target"),
    sorted(VALID_TRANSITIONS, key=lambda edge: (edge[0].value, edge[1].value)),
)
def test_validate_transition_accepts_each_explicit_edge(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    INVALID_TRANSITIONS,
)
def test_validate_transition_rejects_every_implicit_edge(
    current: AttemptStatus, target: AttemptStatus
) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_transition(current, target)


def test_plan_starts_at_classify() -> None:
    decision = plan_next(snapshot())

    assert decision.kind is DecisionKind.CLAIM
    assert decision.stage is StageName.CLASSIFY
    assert decision.attempt_number == 1
    assert decision.input_hash == INPUTS[StageName.CLASSIFY]


@pytest.mark.parametrize("status", [AttemptStatus.COMPLETED, AttemptStatus.SKIPPED])
def test_completed_or_skipped_stage_advances(status: AttemptStatus) -> None:
    decision = plan_next(snapshot(attempt(StageName.CLASSIFY, status)))

    assert decision.kind is DecisionKind.CLAIM
    assert decision.stage is StageName.EXTRACT


def test_identical_completed_input_is_a_noop() -> None:
    decision = plan_next(
        snapshot(
            attempt(StageName.CLASSIFY, AttemptStatus.COMPLETED),
            attempt(StageName.EXTRACT, AttemptStatus.COMPLETED),
        )
    )

    assert decision.stage is StageName.RECONCILE
    assert decision.attempt_number == 1


def test_changed_input_restarts_affected_stage_without_rerunning_upstream() -> None:
    attempts = (
        attempt(StageName.CLASSIFY, AttemptStatus.COMPLETED),
        attempt(StageName.EXTRACT, AttemptStatus.COMPLETED),
        attempt(StageName.RECONCILE, AttemptStatus.COMPLETED, input_hash="sha256:old"),
        attempt(StageName.CHECK_RULES, AttemptStatus.COMPLETED),
    )

    decision = plan_next(snapshot(*attempts))

    assert decision.kind is DecisionKind.CLAIM
    assert decision.stage is StageName.RECONCILE
    assert decision.attempt_number == 2


def test_persisted_invalidation_recomputes_downstream_even_when_hash_is_unchanged() -> None:
    attempts = (
        attempt(StageName.CLASSIFY, AttemptStatus.COMPLETED),
        attempt(StageName.EXTRACT, AttemptStatus.COMPLETED),
        attempt(StageName.RECONCILE, AttemptStatus.COMPLETED),
        attempt(
            StageName.CHECK_RULES,
            AttemptStatus.COMPLETED,
            invalidated_at=NOW - timedelta(seconds=1),
        ),
        attempt(
            StageName.DRAFT,
            AttemptStatus.COMPLETED,
            invalidated_at=NOW - timedelta(seconds=1),
        ),
    )

    decision = plan_next(snapshot(*attempts))

    assert decision.stage is StageName.CHECK_RULES
    assert decision.attempt_number == 2


def test_live_lease_waits_and_expired_lease_is_reclaimed() -> None:
    live = plan_next(
        snapshot(
            attempt(
                StageName.CLASSIFY,
                AttemptStatus.RUNNING,
                lease_expires_at=NOW + timedelta(seconds=1),
            )
        )
    )
    expired = plan_next(
        snapshot(
            attempt(
                StageName.CLASSIFY,
                AttemptStatus.RUNNING,
                lease_expires_at=NOW - timedelta(microseconds=1),
            )
        )
    )

    assert live.kind is DecisionKind.WAIT
    assert expired.kind is DecisionKind.RECLAIM
    assert expired.attempt_number == 1


def test_retry_waits_until_due_then_uses_a_new_attempt_number() -> None:
    not_due = plan_next(
        snapshot(
            attempt(
                StageName.CLASSIFY,
                AttemptStatus.RETRY,
                next_attempt_at=NOW + timedelta(seconds=1),
            )
        )
    )
    due = plan_next(
        snapshot(
            attempt(
                StageName.CLASSIFY,
                AttemptStatus.RETRY,
                next_attempt_at=NOW,
            )
        )
    )

    assert not_due.kind is DecisionKind.WAIT
    assert due.kind is DecisionKind.RETRY
    assert due.attempt_number == 2


def test_escalation_and_all_completed_stages_are_review_ready() -> None:
    escalated = plan_next(
        snapshot(attempt(StageName.CLASSIFY, AttemptStatus.ESCALATED))
    )
    completed = plan_next(
        snapshot(
            *(attempt(stage, AttemptStatus.COMPLETED) for stage in StageName)
        )
    )

    assert escalated.kind is DecisionKind.REVIEW_READY
    assert completed.kind is DecisionKind.REVIEW_READY


@pytest.mark.parametrize(
    ("stage", "result"),
    [
        (
            StageName.CLASSIFY,
            StageResult(status="completed", artifact_hashes=(), next_stage=StageName.DRAFT),
        ),
        (
            StageName.EXTRACT,
            StageResult(status="retry", artifact_hashes=(), next_stage=StageName.RECONCILE),
        ),
        (
            StageName.DRAFT,
            StageResult(status="escalated", artifact_hashes=(), next_stage=StageName.DRAFT),
        ),
    ],
)
def test_stage_result_rejects_invalid_next_stage(
    stage: StageName, result: StageResult
) -> None:
    with pytest.raises(InvalidTransitionError):
        validate_stage_result(stage, result)


@pytest.mark.parametrize("status", ["retry", "skipped", "escalated"])
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_noncompleted_stage_result_requires_a_nonblank_reason(
    status: str, reason: str | None
) -> None:
    next_stage = StageName.CLASSIFY if status == "retry" else StageName.EXTRACT
    if status == "escalated":
        next_stage = None
    result = StageResult(
        status=status,
        artifact_hashes=(),
        next_stage=next_stage,
        reason=reason,
    )

    with pytest.raises(InvalidTransitionError, match="nonblank reason"):
        validate_stage_result(StageName.CLASSIFY, result)


def test_completed_stage_result_can_omit_reason() -> None:
    validate_stage_result(
        StageName.CLASSIFY,
        StageResult(
            status="completed",
            artifact_hashes=(),
            next_stage=StageName.EXTRACT,
        ),
    )
