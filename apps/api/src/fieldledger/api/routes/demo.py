from typing import Annotated

from fastapi import APIRouter, Header

from fieldledger.api.dependencies import CurrentActor, Security, Services
from fieldledger.api.models import DemoSeedResponse
from fieldledger.domain.models import ContractModel
from fieldledger.security import AuthorizationError

router = APIRouter(prefix="/demo", tags=["demo"])


class DemoSeedRequest(ContractModel):
    pass


@router.post(
    "/seed",
    response_model=DemoSeedResponse,
    operation_id="seed_demo",
)
async def seed_demo(
    body: DemoSeedRequest,
    actor: CurrentActor,
    manager: Security,
    container: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> object:
    del body
    if not manager.local_demo_enabled or container.demo is None:
        raise AuthorizationError("local demo mode is disabled")
    return await container.demo.seed(actor=actor, idempotency_key=idempotency_key)
