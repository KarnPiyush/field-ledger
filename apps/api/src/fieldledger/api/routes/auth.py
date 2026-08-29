from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, status
from pydantic import Field

from fieldledger.api.dependencies import Security
from fieldledger.api.models import DemoSessionResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(prefix="/auth", tags=["auth"])


class DemoSessionRequest(ContractModel):
    identity: str = Field(min_length=1)


@router.post(
    "/demo-session",
    status_code=status.HTTP_201_CREATED,
    response_model=DemoSessionResponse,
)
async def demo_session(
    body: DemoSessionRequest,
    manager: Security,
    bootstrap_secret: Annotated[
        str, Header(alias="X-FieldLedger-Demo-Bootstrap", min_length=1)
    ],
) -> DemoSessionResponse:
    now = datetime.now(UTC)
    token = manager.mint_demo_session(
        identity=body.identity,
        bootstrap_secret=bootstrap_secret,
        now=now,
        lifetime=timedelta(hours=8),
    )
    actor = manager.verify_session(token, now=now)
    return DemoSessionResponse(
        access_token=token,
        token_type="bearer",
        actor_id=actor.actor_id,
        source=actor.source.value,
        global_role=actor.global_role.value,
    )
