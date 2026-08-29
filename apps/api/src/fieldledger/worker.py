import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fieldledger.domain.models import StageResult
from fieldledger.persistence.repositories import (
    ClaimedStage,
    PreparedArtifact,
    PreparedDependency,
)
from fieldledger.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageExecution:
    result: StageResult
    artifacts: tuple[PreparedArtifact, ...] = ()
    dependencies: tuple[PreparedDependency, ...] = ()
    duration_ms: int | None = None
    estimated_cost: Decimal | None = None


StageExecutor = Callable[[ClaimedStage], Coroutine[Any, Any, StageExecution]]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


async def _cancel_and_wait(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _maintain_lease(
    service: WorkflowService,
    claim_ref: list[ClaimedStage],
    *,
    clock: Clock,
    heartbeat_interval: float,
) -> None:
    while True:
        await asyncio.sleep(heartbeat_interval)
        claim_ref[0] = await service.heartbeat_stage(claim_ref[0], now=clock())


async def run_worker_iteration(
    service: WorkflowService,
    *,
    worker_id: str,
    execute_stage: StageExecutor,
    run_id: UUID | None = None,
    clock: Clock = utc_now,
    heartbeat_interval: float | None = None,
) -> bool:
    interval = (
        heartbeat_interval
        if heartbeat_interval is not None
        else service.lease_duration.total_seconds() / 3
    )
    if interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    claim = await service.claim_stage(worker_id=worker_id, run_id=run_id, now=clock())
    if claim is None:
        return False

    claim_ref = [claim]
    execution_task: asyncio.Task[StageExecution] = asyncio.create_task(execute_stage(claim))
    heartbeat_task = asyncio.create_task(
        _maintain_lease(
            service,
            claim_ref,
            clock=clock,
            heartbeat_interval=interval,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {execution_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            await _cancel_and_wait(execution_task)
            if heartbeat_error is None:
                raise RuntimeError("lease heartbeat stopped unexpectedly")
            raise heartbeat_error

        finished_at = clock()
        try:
            execution = execution_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await _cancel_and_wait(heartbeat_task)
            logger.exception(
                "Stage %s failed",
                claim.stage.value,
                exc_info=(type(error), error, error.__traceback__),
            )
            await service.fail_stage(claim_ref[0], error, now=finished_at)
            return True

        await _cancel_and_wait(heartbeat_task)
        await service.commit_stage(
            claim_ref[0],
            execution.result,
            artifacts=execution.artifacts,
            dependencies=execution.dependencies,
            duration_ms=execution.duration_ms,
            estimated_cost=execution.estimated_cost,
            now=finished_at,
        )
        return True
    finally:
        await _cancel_and_wait(execution_task)
        await _cancel_and_wait(heartbeat_task)


async def run_worker(
    service: WorkflowService,
    *,
    worker_id: str,
    execute_stage: StageExecutor,
    poll_interval: float = 1.0,
    clock: Clock = utc_now,
    heartbeat_interval: float | None = None,
) -> None:
    while True:
        processed = await run_worker_iteration(
            service,
            worker_id=worker_id,
            execute_stage=execute_stage,
            clock=clock,
            heartbeat_interval=heartbeat_interval,
        )
        if not processed:
            await asyncio.sleep(poll_interval)


def run() -> None:
    from fieldledger.agent.stages import DeterministicStageExecutor
    from fieldledger.db import session_factory

    service = WorkflowService(session_factory)
    executor = DeterministicStageExecutor(session_factory)
    asyncio.run(
        run_worker(
            service,
            worker_id=f"worker-{os.getpid()}",
            execute_stage=executor,
        )
    )


if __name__ == "__main__":
    run()
