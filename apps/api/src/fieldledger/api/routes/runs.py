from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.models import RunResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(tags=["runs"])


class EmptyStartRunRequest(ContractModel):
    pass


@router.post(
    "/projects/{project_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunResponse,
)
async def start_run(
    project_id: UUID,
    _body: EmptyStartRunRequest,
    actor: CurrentActor,
    container: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> object:
    return await container.workflow.start_run(
        project_id,
        actor=actor,
        idempotency_key=idempotency_key,
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def inspect_run(
    run_id: UUID, actor: CurrentActor, container: Services
) -> object:
    return await container.workflow.inspect_run(run_id, actor)
