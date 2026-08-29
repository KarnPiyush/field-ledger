from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, status

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.errors import ApiError
from fieldledger.api.models import (
    ConfirmationResponse,
    ReviewBatchResponse,
    ReviewListResponse,
)
from fieldledger.domain.models import ContractModel, ReviewDecision
from fieldledger.domain.review import ApplyResult, DecisionSubmission

router = APIRouter(tags=["reviews"])


class EmptyConfirmationRequest(ContractModel):
    pass


class DecisionRequest(ContractModel):
    confirmation_token: str
    decisions: tuple[ReviewDecision, ...]


@router.get("/projects/{project_id}/reviews", response_model=ReviewListResponse)
async def list_reviews(
    project_id: UUID,
    actor: CurrentActor,
    container: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    items, total = await container.reviews.list_reviews(
        project_id, actor, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/reviews/{batch_id}", response_model=ReviewBatchResponse)
async def get_review(
    batch_id: UUID, actor: CurrentActor, container: Services
) -> object:
    return await container.reviews.get_batch(batch_id, actor)


@router.post(
    "/reviews/{batch_id}/confirmation",
    status_code=status.HTTP_201_CREATED,
    response_model=ConfirmationResponse,
)
async def request_confirmation(
    batch_id: UUID,
    _body: EmptyConfirmationRequest,
    actor: CurrentActor,
    container: Services,
) -> object:
    return await container.reviews.request_confirmation(batch_id, actor)


@router.post("/reviews/{batch_id}/decisions", response_model=ApplyResult)
async def submit_decisions(
    batch_id: UUID,
    body: DecisionRequest,
    actor: CurrentActor,
    container: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> ApplyResult:
    result = cast(
        ApplyResult,
        await container.reviews.submit_decisions(
            batch_id,
            DecisionSubmission(
                idempotency_key=idempotency_key,
                confirmation_token=body.confirmation_token,
                decisions=body.decisions,
            ),
            actor,
        ),
    )
    if result.status == "stale":
        raise ApiError(
            status=409,
            code="review-stale",
            title="Review is stale",
            detail="Review dependencies changed after confirmation.",
            extensions={
                "changes": [change.model_dump(mode="json") for change in result.stale_changes]
            },
        )
    return result
