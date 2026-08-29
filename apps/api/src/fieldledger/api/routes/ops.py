from uuid import UUID

from fastapi import APIRouter

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.models import ProviderJobResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(prefix="/ops", tags=["ops"])


class EmptyPollRequest(ContractModel):
    pass


@router.get("/provider-jobs/{job_id}", response_model=ProviderJobResponse)
async def get_provider_job(
    job_id: UUID, actor: CurrentActor, container: Services
) -> object:
    return await container.providers.get_job(job_id, actor)


@router.post("/provider-jobs/{job_id}/poll", response_model=ProviderJobResponse)
async def poll_provider_job(
    job_id: UUID,
    _body: EmptyPollRequest,
    actor: CurrentActor,
    container: Services,
) -> object:
    return await container.providers.poll(job_id, actor)
