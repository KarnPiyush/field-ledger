from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status
from pydantic import Field

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.models import ProviderJobResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(tags=["documents"])


class StartEditRequest(ContractModel):
    workflow_run_id: UUID
    prompt: str = Field(min_length=1)


@router.post(
    "/documents/{document_id}/edits",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ProviderJobResponse,
)
async def start_edit(
    document_id: UUID,
    body: StartEditRequest,
    actor: CurrentActor,
    container: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> object:
    return await container.providers.start_edit(
        document_id=document_id,
        workflow_run_id=body.workflow_run_id,
        prompt=body.prompt,
        idempotency_key=idempotency_key,
        actor=actor,
    )


@router.get(
    "/documents/{document_id}/edits/by-idempotency-key",
    response_model=ProviderJobResponse,
    operation_id="lookup_edit_by_idempotency_key",
)
async def lookup_edit_by_idempotency_key(
    document_id: UUID,
    actor: CurrentActor,
    container: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> object:
    return await container.providers.lookup_edit(
        document_id=document_id,
        idempotency_key=idempotency_key,
        actor=actor,
    )
